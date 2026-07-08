"""Sweeper: доматчивает застрявшие СВХ/ДО1-сообщения (cargo=None).

Проблема: CMN.13010 (регистрация ДО1) / CMN.13029 (представление) приходят,
но остаются cargo=None — не привязались. Основная причина — гонка
«сообщение прилетело РАНЬШЕ, чем партия (Cargo) создана в CargoTrack»:
в момент dispatch match не нашёл Cargo, а планового перематча не было.
Итог — у партии в «Общее» пусто по ДО1/СВХ, хотя данные (лицензия, дата,
рег.номер) реально пришли (на 08.07.2026 — 13 таких партий).

match_svh_do1 теперь умеет прямой матч по svh_mawb → Cargo.awb_number,
поэтому повторный dispatch привязывает такие сообщения. Этот sweeper
находит их (только те, где svh_mawb соответствует СУЩЕСТВУЮЩЕМУ Cargo —
чтобы не гонять чужие) и передиспатчивает.

Идемпотентно, durable — можно кроном:
    manage.py redispatch_unmatched_svh              # dry-run
    manage.py redispatch_unmatched_svh --apply
    manage.py redispatch_unmatched_svh --apply --limit 20
"""
from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, Cargo

SVH_KINDS = ['svh_placed', 'svh_do1_registered']


class Command(BaseCommand):
    help = 'Доматчивает застрявшие СВХ/ДО1-сообщения (cargo=None) по svh_mawb.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально передиспатчить (без флага — dry-run)')
        parser.add_argument('--limit', type=int, default=0,
                            help='Сколько за прогон (0 = все)')

    def handle(self, *args, **opts):
        from cargo.services.alta.inbox import dispatch, _retry_on_locked

        # Множество awb_number существующих партий — чтобы отобрать только
        # доматчиваемые (svh_mawb указывает на нашу партию), не гоняя чужие.
        cargo_awbs = {a.strip().lower()
                      for a in Cargo.objects.values_list('awb_number', flat=True)
                      if a}

        qs = (AltaInboxMessage.objects
              .filter(cargo__isnull=True, msg_kind__in=SVH_KINDS)
              .order_by('prepared_at'))

        candidates = []
        for m in qs.iterator():
            pm = m.parsed_meta or {}
            mawb = (pm.get('svh_mawb') or pm.get('svh_mawb_raw') or '').strip()
            if mawb and mawb.lower() in cargo_awbs:
                candidates.append(m)
                if opts['limit'] and len(candidates) >= opts['limit']:
                    break

        self.stdout.write(f'доматчиваемых СВХ-сообщений (svh_mawb = наш Cargo): '
                          f'{len(candidates)}')
        if not candidates:
            return
        if not opts['apply']:
            for m in candidates[:30]:
                mawb = (m.parsed_meta or {}).get('svh_mawb', '')
                self.stdout.write(f'  #{m.pk} {m.msg_type} svh_mawb={mawb!r} '
                                  f'recv={str(m.received_at)[:16]}')
            self.stdout.write('(dry-run — добавь --apply)')
            return

        matched = still = err = 0
        for m in candidates:
            try:
                _retry_on_locked(dispatch, m)
                m.refresh_from_db(fields=['cargo'])
                if m.cargo_id:
                    matched += 1
                else:
                    still += 1
            except Exception as e:  # noqa: BLE001
                err += 1
                self.stderr.write(f'  #{m.pk}: {e}')

        self.stdout.write(self.style.SUCCESS(
            f'привязано {matched}, осталось {still}, ошибок {err}'))
