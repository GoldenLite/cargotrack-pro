"""Рассылка авто-комакта на СВХ Внуково по свежим выпускам (svh-komakt-automation).

На каждый выпущенный ИМПОРТ на нашем СВХ Внуково (лицензия 10001) формируем
Excel-разрез товаров поданной ДТЭГ, агрегированный по ТН ВЭД, и шлём на почту
СВХ. Заменяет ручную работу декларанта. Дедуп/аудит — KomaktNotification.

Гейты:
  • KOMAKT_MAILER_ENABLED — общий выключатель (.env). Без него конвейерный
    прогон ничего не делает (ручной --hawb работает всегда).
  • KOMAKT_MAILER_SINCE — cutover: шлём только выпуски с release_date >= метки.
  • KOMAKT_MAILER_CAP — максимум писем за прогон.
  • KOMAKT_MAILER_MAX_ATTEMPTS — предел повторов.
  • KOMAKT_MAILER_TO — получатель (на тесте — andylapshin@yandex.ru).

    manage.py send_svh_komakts                       # конвейерный прогон
    manage.py send_svh_komakts --dry-run             # показать, не слать
    manage.py send_svh_komakts --hawb 10309592678    # ручной, одна накладная
    manage.py send_svh_komakts --hawb X --to a@b.ru --force
"""
from __future__ import annotations

import datetime

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand
from django.utils import timezone

XLSX_MIME = ('application/vnd.openxmlformats-officedocument.'
             'spreadsheetml.sheet')


def _recipients(raw: str) -> list:
    """Список получателей из строки KOMAKT_MAILER_TO / --to (через запятую или
    точку с запятой). Позволяет слать на несколько адресов одновременно."""
    import re
    return [a.strip() for a in re.split(r'[;,]', raw or '') if a.strip()]


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
    help = 'Рассылка авто-комакта на СВХ Внуково по свежим выпускам.'

    def add_arguments(self, parser):
        parser.add_argument('--since', default=None)
        parser.add_argument('--to', default=None)
        parser.add_argument('--limit', type=int, default=None)
        parser.add_argument('--hawb', default='',
                            help='Ручной прогон одной накладной (минуя enabled/cutover)')
        parser.add_argument('--force', action='store_true',
                            help='Слать даже если уже SENT (для --hawb)')
        parser.add_argument('--dry-run', action='store_true')

    def _process_one(self, h, to, dry, force=False):
        """→ 'sent'|'dry'|'skipped'|'excluded'|'failed'|'already'."""
        from cargo.models import KomaktNotification
        from cargo.services.alta.svh_komakt import is_vnukovo_import, build_komakt_xlsx

        hn = h.hawb_number
        notif = KomaktNotification.objects.filter(hawb_number=hn).first()
        if notif and notif.status == KomaktNotification.STATUS_SENT and not force:
            return 'already'

        # Только наш СВХ Внуково + импорт — иначе терминально исключаем.
        if not is_vnukovo_import(h):
            if dry:
                self.stdout.write(f'  DRY-EXCLUDE {hn}: не Внуково-импорт')
                return 'excluded'
            if notif is None:
                notif = KomaktNotification(hawb_number=hn)
            notif.status = KomaktNotification.STATUS_EXCLUDED
            notif.error = 'не Внуково-импорт (лицензия != 10001 или экспорт)'
            notif.attempts = (notif.attempts or 0) + 1
            notif.save()
            self.stdout.write(f'  EXCLUDE {hn}: не Внуково-импорт')
            return 'excluded'

        xlsx, meta = build_komakt_xlsx(hn)
        if xlsx is None:
            reason = (meta or {}).get('reason', 'не собрать комакт')
            if dry:
                self.stdout.write(f'  DRY-SKIP {hn}: {reason}')
                return 'skipped'
            if notif is None:
                notif = KomaktNotification(hawb_number=hn)
            notif.attempts = (notif.attempts or 0) + 1
            notif.to_email = to
            notif.release_date = h.release_date
            # «регулярная ДТ» — пока терминально исключаем (нужен отдельный парсер)
            notif.status = (KomaktNotification.STATUS_EXCLUDED
                            if 'регулярная ДТ' in reason
                            else KomaktNotification.STATUS_SKIPPED)
            notif.error = reason
            notif.save()
            self.stdout.write(f'  SKIP {hn}: {reason} (попытка {notif.attempts})')
            return 'excluded' if notif.status == KomaktNotification.STATUS_EXCLUDED else 'skipped'

        reg = h.customs_declaration_number or ''
        if dry:
            self.stdout.write(
                f'  DRY {hn}: {meta["num_positions"]} поз → {meta["num_codes"]} кодов, '
                f'Σ{meta["total_gross_kg"]}кг/{meta["total_value"]}{meta["currency"]} → {to}')
            return 'dry'

        if notif is None:
            notif = KomaktNotification(hawb_number=hn)
        notif.to_email = to
        notif.release_date = h.release_date
        notif.registration_number = reg
        notif.num_positions = meta['num_positions']
        notif.num_codes = meta['num_codes']
        notif.attempts = (notif.attempts or 0) + 1

        mawb = meta.get('mawb') or ''
        subj_ident = (f'партия {mawb} / накл {hn}' if mawb else f'накл {hn}')
        body_ident = (f'по накладной {hn} / партия {mawb}' if mawb
                      else f'по накладной {hn}')
        subject = 'Коммерческий акт ' + subj_ident
        try:
            msg = EmailMessage(
                subject=subject,
                body=(f'Коммерческий акт {body_ident}.\n'
                      f'Агрегировано по коду ТН ВЭД: {meta["num_positions"]} '
                      f'позиций → {meta["num_codes"]} строк.\n'
                      f'Регистрационный номер ДТ: {reg or "—"}.'),
                to=[to])
            msg.to = _recipients(to)
            msg.attach(f'komakt_{hn}.xlsx', xlsx, XLSX_MIME)
            msg.send(fail_silently=False)
        except Exception as e:
            notif.status = KomaktNotification.STATUS_FAILED
            notif.error = f'send: {e}'[:2000]
            notif.save()
            self.stdout.write(self.style.ERROR(f'  SEND FAIL {hn}: {e}'))
            return 'failed'

        notif.status = KomaktNotification.STATUS_SENT
        notif.sent_at = timezone.now()
        notif.error = ''
        notif.save()
        self.stdout.write(self.style.SUCCESS(
            f'  SENT {hn} → {to} ({meta["num_positions"]}→{meta["num_codes"]})'))
        return 'sent'

    def handle(self, *args, **opts):
        from cargo.models import HouseWaybill, KomaktNotification

        to = opts['to'] or getattr(settings, 'KOMAKT_MAILER_TO', '')
        if not to:
            self.stdout.write(self.style.WARNING('Получатель не задан (KOMAKT_MAILER_TO) — выход'))
            return

        if opts['hawb']:
            h = HouseWaybill.objects.filter(hawb_number=opts['hawb']).order_by('-id').first()
            if not h:
                self.stdout.write(self.style.WARNING(f'HAWB {opts["hawb"]} не найдена'))
                return
            res = self._process_one(h, to, opts['dry_run'], force=opts['force'])
            self.stdout.write(f'Готово: {opts["hawb"]} → {res}')
            return

        if not getattr(settings, 'KOMAKT_MAILER_ENABLED', False):
            self.stdout.write('KOMAKT_MAILER_ENABLED=False — рассылка выключена, пропуск')
            return

        cap = opts['limit'] or getattr(settings, 'KOMAKT_MAILER_CAP', 25)
        max_attempts = getattr(settings, 'KOMAKT_MAILER_MAX_ATTEMPTS', 6)
        since = _parse_since(opts['since'] or getattr(settings, 'KOMAKT_MAILER_SINCE', ''))
        if since is None:
            self.stdout.write(self.style.WARNING(
                'KOMAKT_MAILER_SINCE не задан — во избежание рассылки по историческому '
                'бэклогу выхожу. Задай cutover-метку в .env.'))
            return

        done = (KomaktNotification.objects
                .filter(status__in=[KomaktNotification.STATUS_SENT,
                                    KomaktNotification.STATUS_EXCLUDED])
                .values_list('hawb_number', flat=True))
        exhausted = (KomaktNotification.objects
                     .filter(attempts__gte=max_attempts)
                     .exclude(status=KomaktNotification.STATUS_SENT)
                     .values_list('hawb_number', flat=True))
        # Отбор на уровне БД: выпущенный импорт на Внуково (10001).
        candidates = (HouseWaybill.objects
                      .filter(customs_status='RELEASED', release_date__gte=since,
                              mawb__warehouse_license__startswith='10001')
                      .exclude(shipment_type='EXPORT')
                      .exclude(hawb_number__in=list(done))
                      .exclude(hawb_number__in=list(exhausted))
                      .order_by('release_date'))

        counts = {'sent': 0, 'skipped': 0, 'failed': 0, 'dry': 0, 'already': 0, 'excluded': 0}
        processed = 0
        for h in candidates.iterator():
            if counts['sent'] + counts['dry'] >= cap:
                self.stdout.write(self.style.WARNING(
                    f'Достигнут кап {cap} писем/прогон — остальные в следующий прогон'))
                break
            res = self._process_one(h, to, opts['dry_run'])
            counts[res] = counts.get(res, 0) + 1
            processed += 1

        self.stdout.write(self.style.SUCCESS(
            f'send_svh_komakts: обработано {processed}, отправлено {counts["sent"]}, '
            f'dry {counts["dry"]}, отложено {counts["skipped"]}, исключено {counts["excluded"]}, '
            f'ошибок {counts["failed"]}, уже были {counts["already"]}'))
