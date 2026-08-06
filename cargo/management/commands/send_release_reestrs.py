"""Рассылка per-HAWB реестров ДТЭГ по свежим выпускам (release-reestr-mailer).

Отдельный поток, независимый от службы «Регистрация» Альты: на каждый
выпущенный груз (HouseWaybill.customs_status=RELEASED) формируем реестр ДТЭГ
из поданной декларации (наша БД) и шлём PDF на заданную почту. Дедуп и аудит —
через ReleaseReestrNotification (терминальный статус SENT = «уже отправлено»).

Гейты:
  • REESTR_MAILER_ENABLED — общий выключатель (в .env). Без него конвейерный
    прогон ничего не делает (ручной --hawb работает всегда).
  • REESTR_MAILER_SINCE (cutover) — шлём только выпуски с release_date >= метки.
    Защита от «выстрела» по историческому бэклогу при первом включении.
  • REESTR_MAILER_CAP — максимум писем за прогон (флуд/SMTP-лимит).
  • REESTR_MAILER_MAX_ATTEMPTS — предел повторов (нет подачи / упала отправка).

    manage.py send_release_reestrs                      # конвейерный прогон
    manage.py send_release_reestrs --dry-run            # показать, не слать
    manage.py send_release_reestrs --hawb 10287546687   # ручной, одна накладная
    manage.py send_release_reestrs --hawb X --force     # переслать, даже если SENT
"""
from __future__ import annotations

import datetime

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand
from django.utils import timezone


def _parse_since(raw: str):
    """ISO-строка cutover → aware datetime (или None если пусто/невалидно)."""
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


def _release_iso(h) -> str:
    if not getattr(h, 'release_date', None):
        return ''
    rd = h.release_date
    return rd.isoformat() if hasattr(rd, 'isoformat') else str(rd)


class Command(BaseCommand):
    help = 'Рассылка per-HAWB реестров ДТЭГ по свежим выпускам.'

    def add_arguments(self, parser):
        parser.add_argument('--since', default=None,
                            help='ISO cutover (по умолчанию settings.REESTR_MAILER_SINCE)')
        parser.add_argument('--to', default=None,
                            help='Получатель (по умолчанию settings.REESTR_MAILER_TO)')
        parser.add_argument('--limit', type=int, default=None,
                            help='Кап писем за прогон (по умолчанию settings.REESTR_MAILER_CAP)')
        parser.add_argument('--hawb', default='',
                            help='Ручной прогон одной накладной (игнорит enabled/cutover)')
        parser.add_argument('--force', action='store_true',
                            help='Слать даже если уже SENT (для --hawb)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Не слать и не помечать SENT — только показать')

    # ── отрисовка + отправка одной накладной ──────────────────────────────
    def _process_one(self, h, to, dry, force=False):
        """Возвращает 'sent' | 'dry' | 'skipped' | 'excluded' | 'failed' | 'already'.

        В режиме dry в БД НЕ пишем (не создаём строк, не крутим attempts) —
        только показываем, что было бы отправлено.
        """
        from cargo.models import ReleaseReestrNotification
        from cargo.services.alta.dteg_reestr import (
            find_submission_for_hawb, is_correspondence_hawb, CORRESPONDENCE_TNVED)
        from cargo.services.alta.dteg_reestr_xslt import render_pdf as xslt_render_pdf
        from cargo.services.alta.dteg_reestr_xslt import find_release_message

        hn = h.hawb_number
        notif = ReleaseReestrNotification.objects.filter(hawb_number=hn).first()
        if notif and notif.status == ReleaseReestrNotification.STATUS_SENT and not force:
            return 'already'

        # Наличие поданной ДТЭГ (без неё реестр не собрать) + тип сообщения.
        rx, mt, _obs = find_submission_for_hawb(hn)
        # Корреспонденция: 1 товар с ТН ВЭД 4911990000 — отдельный ДТЭГ на такую
        # накладную не требуется → терминально исключаем из рассылки.
        is_corr = bool(rx) and is_correspondence_hawb(hn, rx)
        # Слать ТОЛЬКО по решению ВЫПУСК (DecisionCode=10). Без code-10 CMN.11350
        # — не выпуск (или ещё нет отметки) → не шлём.
        has_release = find_release_message(hn) is not None

        # Dry-run: ничего не пишем в БД.
        if dry:
            if not rx:
                self.stdout.write(f'  DRY-SKIP {hn}: подача ДТЭГ не найдена')
                return 'skipped'
            if is_corr:
                self.stdout.write(f'  DRY-EXCLUDE {hn}: корреспонденция (1 товар, ТН ВЭД {CORRESPONDENCE_TNVED})')
                return 'excluded'
            if not has_release:
                self.stdout.write(f'  DRY-SKIP {hn}: нет решения о выпуске (DecisionCode=10)')
                return 'skipped'
            self.stdout.write(f'  DRY {hn}: PDF будет собран → {to} ({mt})')
            return 'dry'

        # Реальный прогон: создаём/обновляем строку и считаем попытку.
        if notif is None:
            notif = ReleaseReestrNotification(hawb_number=hn)
        notif.to_email = to
        notif.release_date = h.release_date
        notif.attempts = (notif.attempts or 0) + 1

        # Корреспонденция — терминально исключаем (больше не трогаем).
        if is_corr:
            notif.status = ReleaseReestrNotification.STATUS_EXCLUDED
            notif.error = (f'корреспонденция: 1 товар, ТН ВЭД {CORRESPONDENCE_TNVED} '
                           f'— отдельный ДТЭГ не требуется')
            notif.save()
            self.stdout.write(f'  EXCLUDE {hn}: корреспонденция (ТН ВЭД {CORRESPONDENCE_TNVED})')
            return 'excluded'

        if not has_release:
            notif.status = ReleaseReestrNotification.STATUS_SKIPPED
            notif.error = 'нет решения о выпуске (DecisionCode=10) в CMN.11350'
            notif.save()
            self.stdout.write(f'  SKIP {hn}: нет DecisionCode=10 (попытка {notif.attempts})')
            return 'skipped'

        if not rx:
            notif.status = ReleaseReestrNotification.STATUS_SKIPPED
            notif.error = 'подача ДТЭГ не найдена в БД'
            notif.save()
            self.stdout.write(f'  SKIP {hn}: подача ДТЭГ не найдена (попытка {notif.attempts})')
            return 'skipped'

        # Рендер per-HAWB реестра РОДНЫМ шаблоном Альты (XSLT) → Edge → PDF.
        try:
            pdf = xslt_render_pdf(hn)
        except Exception as e:
            notif.status = ReleaseReestrNotification.STATUS_FAILED
            notif.error = f'render: {e}'[:2000]
            notif.save()
            self.stdout.write(self.style.ERROR(f'  RENDER FAIL {hn}: {e}'))
            return 'failed'
        if not pdf:
            notif.status = ReleaseReestrNotification.STATUS_SKIPPED
            notif.error = 'рендер не собрал PDF (нет подачи/накладной)'
            notif.save()
            self.stdout.write(f'  SKIP {hn}: рендер пустой (попытка {notif.attempts})')
            return 'skipped'

        reg = h.customs_declaration_number or ''
        subject = f'Реестр ДТЭГ {hn}' + (f' — {reg}' if reg else '')
        try:
            msg = EmailMessage(
                subject=subject,
                body=(f'Реестр ДТЭГ по накладной {hn}.\n'
                      f'Регистрационный номер ДТ: {reg or "—"}.'),
                to=[to])
            msg.attach(f'reestr_{hn}.pdf', pdf, 'application/pdf')
            msg.send(fail_silently=False)
        except Exception as e:
            notif.status = ReleaseReestrNotification.STATUS_FAILED
            notif.error = f'send: {e}'[:2000]
            notif.save()
            self.stdout.write(self.style.ERROR(f'  SEND FAIL {hn}: {e}'))
            return 'failed'

        notif.status = ReleaseReestrNotification.STATUS_SENT
        notif.msg_type = mt or ''
        notif.registration_number = reg
        notif.sent_at = timezone.now()
        notif.error = ''
        notif.save()
        self.stdout.write(self.style.SUCCESS(f'  SENT {hn} → {to} ({mt})'))
        return 'sent'

    def handle(self, *args, **opts):
        from cargo.models import HouseWaybill, ReleaseReestrNotification

        to = opts['to'] or getattr(settings, 'REESTR_MAILER_TO', '')
        if not to:
            self.stdout.write(self.style.WARNING('Получатель не задан (REESTR_MAILER_TO) — выход'))
            return

        # ── Ручной режим: одна накладная, минуя enabled/cutover ──
        if opts['hawb']:
            h = HouseWaybill.objects.filter(hawb_number=opts['hawb']).order_by('-id').first()
            if not h:
                self.stdout.write(self.style.WARNING(f'HAWB {opts["hawb"]} не найдена'))
                return
            res = self._process_one(h, to, opts['dry_run'], force=opts['force'])
            self.stdout.write(f'Готово: {opts["hawb"]} → {res}')
            return

        # ── Конвейерный режим — под общим выключателем ──
        if not getattr(settings, 'REESTR_MAILER_ENABLED', False):
            self.stdout.write('REESTR_MAILER_ENABLED=False — рассылка выключена, пропуск')
            return

        cap = opts['limit'] or getattr(settings, 'REESTR_MAILER_CAP', 25)
        max_attempts = getattr(settings, 'REESTR_MAILER_MAX_ATTEMPTS', 6)
        since = _parse_since(opts['since'] or getattr(settings, 'REESTR_MAILER_SINCE', ''))
        if since is None:
            self.stdout.write(self.style.WARNING(
                'REESTR_MAILER_SINCE не задан — во избежание рассылки по историческому '
                'бэклогу выхожу. Задай cutover-метку в .env.'))
            return

        # Исключаем уже отправленные, терминально исключённые и исчерпавшие попытки.
        done = (ReleaseReestrNotification.objects
                .filter(status__in=[ReleaseReestrNotification.STATUS_SENT,
                                    ReleaseReestrNotification.STATUS_EXCLUDED])
                .values_list('hawb_number', flat=True))
        exhausted = (ReleaseReestrNotification.objects
                     .filter(attempts__gte=max_attempts)
                     .exclude(status=ReleaseReestrNotification.STATUS_SENT)
                     .values_list('hawb_number', flat=True))
        # Шлём по ВСЕМ реестрам — и импорт (ИМ/ДТЭГ), и экспорт (ЭК/ПТДЭГ).
        # Фильтра по shipment_type больше нет; отбор — по наличию выпуска
        # (DecisionCode=10 в CMN.11350) внутри _process_one.
        candidates = (HouseWaybill.objects
                      .filter(customs_status='RELEASED', release_date__gte=since)
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
            f'send_release_reestrs: обработано {processed}, отправлено {counts["sent"]}, '
            f'dry {counts["dry"]}, отложено {counts["skipped"]}, исключено {counts["excluded"]}, '
            f'ошибок {counts["failed"]}, уже были {counts["already"]}'))
