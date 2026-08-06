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
        """Возвращает 'sent' | 'dry' | 'skipped' | 'failed' | 'already'."""
        from cargo.models import ReleaseReestrNotification
        from cargo.services.alta.dteg_reestr import reestr_data_for_hawb
        from cargo.services.alta.dteg_reestr_pdf import render_reestr_pdf

        hn = h.hawb_number
        notif, _created = ReleaseReestrNotification.objects.get_or_create(
            hawb_number=hn,
            defaults={'to_email': to, 'status': ReleaseReestrNotification.STATUS_SKIPPED})
        if notif.status == ReleaseReestrNotification.STATUS_SENT and not force:
            return 'already'

        data = reestr_data_for_hawb(hn)
        notif.to_email = to
        notif.release_date = h.release_date
        notif.attempts = (notif.attempts or 0) + 1

        if not data:
            notif.status = ReleaseReestrNotification.STATUS_SKIPPED
            notif.error = 'подача ДТЭГ не найдена в БД'
            notif.save()
            self.stdout.write(f'  SKIP {hn}: подача ДТЭГ не найдена (попытка {notif.attempts})')
            return 'skipped'

        master = ''
        try:
            if getattr(h, 'mawb_id', None):
                master = h.mawb.awb_number or ''
        except Exception:
            master = ''
        try:
            pdf = render_reestr_pdf(
                data, master_awb=master,
                registration_number=h.customs_declaration_number or '',
                release_datetime=_release_iso(h))
        except Exception as e:
            notif.status = ReleaseReestrNotification.STATUS_FAILED
            notif.error = f'render: {e}'[:2000]
            notif.save()
            self.stdout.write(self.style.ERROR(f'  RENDER FAIL {hn}: {e}'))
            return 'failed'

        reg = h.customs_declaration_number or ''
        subject = f'Реестр ДТЭГ {hn}' + (f' — {reg}' if reg else '')
        if dry:
            self.stdout.write(f'  DRY {hn}: PDF {len(pdf)}b → {to} ({data["msg_type"]}, '
                              f'товаров {len(data["house_shipment"].get("goods", []))})')
            return 'dry'

        try:
            msg = EmailMessage(
                subject=subject,
                body=(f'Реестр ДТЭГ по накладной {hn}.\n'
                      f'Регистрационный номер ДТ: {reg or "—"}.\n'
                      f'Сформировано автоматически системой CargoTrack при выпуске груза '
                      f'на основании поданной ДТЭГ ({data["msg_type"]}).'),
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
        notif.msg_type = data['msg_type']
        notif.registration_number = reg
        notif.sent_at = timezone.now()
        notif.error = ''
        notif.save()
        self.stdout.write(self.style.SUCCESS(f'  SENT {hn} → {to} ({data["msg_type"]})'))
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

        # Исключаем уже отправленные и исчерпавшие попытки.
        done = (ReleaseReestrNotification.objects
                .filter(status=ReleaseReestrNotification.STATUS_SENT)
                .values_list('hawb_number', flat=True))
        exhausted = (ReleaseReestrNotification.objects
                     .filter(attempts__gte=max_attempts)
                     .exclude(status=ReleaseReestrNotification.STATUS_SENT)
                     .values_list('hawb_number', flat=True))
        candidates = (HouseWaybill.objects
                      .filter(customs_status='RELEASED', release_date__gte=since)
                      .exclude(hawb_number__in=list(done))
                      .exclude(hawb_number__in=list(exhausted))
                      .order_by('release_date'))

        counts = {'sent': 0, 'skipped': 0, 'failed': 0, 'dry': 0, 'already': 0}
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
            f'dry {counts["dry"]}, отложено {counts["skipped"]}, ошибок {counts["failed"]}, '
            f'уже были {counts["already"]}'))
