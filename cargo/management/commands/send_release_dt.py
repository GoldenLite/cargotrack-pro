"""Рассылка регулярных ДТ (печатный бланк-PDF) по полученным от Альты файлам.

Отдельный поток (не ДТЭГ-реестр): бланк ДТ печатает сама Альта, агент кладёт
PDF в C:\\GTDSERV\\ED\\IN_PDF → залив на /api/v1/dt/pdf/ создаёт IncomingDTDocument
(status=pending). Эта команда берёт pending-документы и шлёт их письмом на
заданную почту, тема/текст — по инд.накладной (02021). Дедуп/аудит — в самой
модели IncomingDTDocument. См. dt-mailer в памяти.

    manage.py send_release_dt                 # конвейерный прогон
    manage.py send_release_dt --dry-run       # показать, не слать
    manage.py send_release_dt --id 42         # ручной прогон одного документа
    manage.py send_release_dt --id 42 --force # переслать, даже если SENT
"""
from __future__ import annotations

import datetime

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand
from django.utils import timezone


def _parse_since(raw: str):
    if not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.strip())
    except ValueError:
        try:
            dt = datetime.datetime.strptime(raw.strip()[:10], '%Y-%m-%d')
        except ValueError:
            return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


class Command(BaseCommand):
    help = 'Рассылка регулярных ДТ (бланк-PDF) по полученным от Альты файлам.'

    def add_arguments(self, parser):
        parser.add_argument('--since', default=None,
                            help='ISO cutover (по умолчанию settings.DT_MAILER_SINCE)')
        parser.add_argument('--to', default=None,
                            help='Получатель (по умолчанию settings.DT_MAILER_TO)')
        parser.add_argument('--limit', type=int, default=None,
                            help='Кап писем за прогон (по умолчанию settings.DT_MAILER_CAP)')
        parser.add_argument('--id', type=int, default=None,
                            help='Ручной прогон одного документа по pk (игнорит enabled/cutover)')
        parser.add_argument('--force', action='store_true',
                            help='Слать даже если уже SENT (для --id)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Не слать и не менять статусы — только показать')

    # ── отправка одного документа ──────────────────────────────────────────
    def _process_one(self, doc, to, dry, force=False):
        """Возвращает 'sent' | 'dry' | 'skipped' | 'excluded' | 'failed' | 'already'."""
        from cargo.models import IncomingDTDocument
        from cargo.services.alta.dt_mailer import dt_label_for_doc

        if doc.status == IncomingDTDocument.STATUS_SENT and not force:
            return 'already'

        reg = doc.declaration_number or ''
        # Дедуп по рег.№: если ЭТУ ДТ уже разослали другим файлом — не дублируем.
        if reg and not force:
            dup = (IncomingDTDocument.objects
                   .filter(declaration_number=reg, status=IncomingDTDocument.STATUS_SENT)
                   .exclude(pk=doc.pk).exists())
            if dup:
                if not dry:
                    self._purge_as_dup(doc)
                self.stdout.write(f'  {"DRY-" if dry else ""}EXCLUDE {reg}: дубль (ДТ уже разослана)')
                return 'excluded'

        label, kind = dt_label_for_doc(doc)

        # Читаем PDF с диска.
        pdf = None
        try:
            if doc.pdf:
                with doc.pdf.open('rb') as fh:
                    pdf = fh.read()
        except Exception as e:
            pdf = None
            read_err = str(e)
        else:
            read_err = ''

        if dry:
            if not pdf:
                self.stdout.write(f'  DRY-SKIP {reg or doc.pk}: PDF-файл не найден на диске')
                return 'skipped'
            self.stdout.write(f'  DRY {reg}: → {to} (накладная {label}/{kind}, {doc.size_bytes}Б)')
            return 'dry'

        doc.attempts = (doc.attempts or 0) + 1
        if not pdf:
            doc.status = IncomingDTDocument.STATUS_FAILED
            doc.error = f'PDF не читается с диска: {read_err}'[:2000]
            doc.save(update_fields=['status', 'error', 'attempts', 'updated_at'])
            self.stdout.write(f'  SKIP {reg or doc.pk}: PDF не читается (попытка {doc.attempts})')
            return 'skipped'

        subject = f'ДТ по накладной {label}' + (f' — {reg}' if reg and kind != 'reg' else '')
        body = (f'Декларация на товары по накладной {label}.'
                + (f'\nРегистрационный номер ДТ: {reg}.' if reg else ''))
        attach_name = doc.filename or f'DT_{reg or doc.pk}.pdf'
        try:
            msg = EmailMessage(subject=subject, body=body, to=[to])
            msg.attach(attach_name, pdf, 'application/pdf')
            msg.send(fail_silently=False)
        except Exception as e:
            doc.status = IncomingDTDocument.STATUS_FAILED
            doc.error = f'send: {e}'[:2000]
            doc.save(update_fields=['status', 'error', 'attempts', 'updated_at'])
            self.stdout.write(self.style.ERROR(f'  SEND FAIL {reg or doc.pk}: {e}'))
            return 'failed'

        doc.status = IncomingDTDocument.STATUS_SENT
        doc.to_email = to
        doc.sent_at = timezone.now()
        doc.error = ''
        doc.save(update_fields=['status', 'to_email', 'sent_at', 'error',
                                'attempts', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(f'  SENT {reg} → {to} (накладная {label})'))
        return 'sent'

    def _purge_as_dup(self, doc):
        try:
            doc.pdf.delete(save=False)
        except Exception:
            pass
        from cargo.models import IncomingDTDocument
        doc.status = IncomingDTDocument.STATUS_PURGED
        doc.error = 'дубль: ДТ уже разослана другим файлом'
        doc.purged_at = timezone.now()
        doc.save(update_fields=['pdf', 'status', 'error', 'purged_at', 'updated_at'])

    def handle(self, *args, **opts):
        from cargo.models import IncomingDTDocument

        to = opts['to'] or getattr(settings, 'DT_MAILER_TO', '')
        if not to:
            self.stdout.write(self.style.WARNING('Получатель не задан (DT_MAILER_TO) — выход'))
            return

        # ── Ручной режим: один документ, минуя enabled/cutover ──
        if opts['id']:
            doc = IncomingDTDocument.objects.filter(pk=opts['id']).first()
            if not doc:
                self.stdout.write(self.style.WARNING(f'IncomingDTDocument #{opts["id"]} не найден'))
                return
            res = self._process_one(doc, to, opts['dry_run'], force=opts['force'])
            self.stdout.write(f'Готово: #{opts["id"]} → {res}')
            return

        # ── Конвейерный режим — под общим выключателем ──
        if not getattr(settings, 'DT_MAILER_ENABLED', False):
            self.stdout.write('DT_MAILER_ENABLED=False — рассылка ДТ выключена, пропуск')
            return

        cap = opts['limit'] or getattr(settings, 'DT_MAILER_CAP', 25)
        max_attempts = getattr(settings, 'DT_MAILER_MAX_ATTEMPTS', 12)
        since = _parse_since(opts['since'] or getattr(settings, 'DT_MAILER_SINCE', ''))
        if since is None:
            self.stdout.write(self.style.WARNING(
                'DT_MAILER_SINCE не задан — во избежание рассылки по историческому '
                'бэклогу выхожу. Задай cutover-метку в .env.'))
            return

        candidates = (IncomingDTDocument.objects
                      .filter(status__in=[IncomingDTDocument.STATUS_PENDING,
                                          IncomingDTDocument.STATUS_FAILED],
                              created_at__gte=since,
                              attempts__lt=max_attempts)
                      .order_by('created_at'))

        counts = {'sent': 0, 'skipped': 0, 'failed': 0, 'dry': 0, 'already': 0, 'excluded': 0}
        processed = 0
        for doc in candidates.iterator():
            if counts['sent'] + counts['dry'] >= cap:
                self.stdout.write(self.style.WARNING(
                    f'Достигнут кап {cap} писем/прогон — остальные в следующий прогон'))
                break
            res = self._process_one(doc, to, opts['dry_run'])
            counts[res] = counts.get(res, 0) + 1
            processed += 1

        self.stdout.write(self.style.SUCCESS(
            f'send_release_dt: обработано {processed}, отправлено {counts["sent"]}, '
            f'dry {counts["dry"]}, отложено {counts["skipped"]}, дублей {counts["excluded"]}, '
            f'ошибок {counts["failed"]}'))

        # Ротация: удаляем PDF-файлы давно разосланных ДТ с диска (строки-аудит
        # остаются). Не в dry-режиме.
        if not opts['dry_run']:
            from cargo.services.alta.dt_mailer import purge_sent_dt_docs
            days = getattr(settings, 'DT_MAILER_PURGE_DAYS', 14)
            purged = purge_sent_dt_docs(days)
            if purged:
                self.stdout.write(f'  ротация: удалено PDF-файлов {purged} (старше {days} дн)')
