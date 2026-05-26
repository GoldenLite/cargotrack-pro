"""Backfill: переразобрать unattached CMN.13010 и привязать к Cargo через ReportNumber.

После добавления парсинга DO1ReportLinkData/ReportNumber в xml_extract.py
старые CMN.13010 в БД не имеют do1_link_report_number в parsed_meta —
нужно один раз пробежаться, переразобрать raw_xml и применить новую логику
match_svh_do1.

Запуск:
    python manage.py backfill_do1_links            # перематчить unattached
    python manage.py backfill_do1_links --all      # переразобрать ВСЕ form=1 (даже attached)
    python manage.py backfill_do1_links --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage
from cargo.services.alta.inbox import (apply_svh_do1, match_svh_do1)
from cargo.services.alta.xml_extract import parse_svh_do1_reg


class Command(BaseCommand):
    help = 'Backfill CMN.13010 → Cargo через DO1ReportLinkData/ReportNumber'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true',
                            help='Переразбирать все form=1 (по умолчанию — только без cargo)')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        qs = AltaInboxMessage.objects.filter(
            msg_type='CMN.13010',
            msg_kind='svh_do1_registered',
        )
        if not opts['all']:
            qs = qs.filter(cargo__isnull=True)
        total = qs.count()
        self.stdout.write(f'CMN.13010 form=1 для обработки: {total}')

        reparsed = 0
        matched  = 0
        applied  = 0
        errors   = 0
        for msg in qs.iterator():
            try:
                fresh = parse_svh_do1_reg(msg.raw_xml or '')
                if not fresh.get('do1_link_report_number'):
                    # XML без линка (старый формат?), ничего нового не извлечь
                    continue
                pm = msg.parsed_meta or {}
                changed = False
                for k, v in fresh.items():
                    if v and pm.get(k) != v:
                        pm[k] = v
                        changed = True
                if changed:
                    msg.parsed_meta = pm
                    reparsed += 1

                cargo, _ = match_svh_do1(msg)
                if cargo:
                    matched += 1
                    if opts['dry_run']:
                        self.stdout.write(
                            f'  [DRY] env={msg.envelope_id} → '
                            f'cargo {cargo.pk} ({cargo.awb_number}) '
                            f'report={pm.get("do1_link_report_number")}'
                        )
                        continue
                    msg.cargo = cargo
                    msg.save(update_fields=['cargo', 'parsed_meta'])
                    err = apply_svh_do1(msg, cargo)
                    if err:
                        errors += 1
                        self.stdout.write(self.style.WARNING(
                            f'  apply_svh_do1 error: env={msg.envelope_id}: {err}'))
                    else:
                        applied += 1
                else:
                    if changed and not opts['dry_run']:
                        msg.save(update_fields=['parsed_meta'])
            except Exception as e:
                errors += 1
                self.stdout.write(self.style.ERROR(
                    f'  env={msg.envelope_id}: {e}'))

        self.stdout.write(self.style.SUCCESS(
            f'\nGoodish: reparsed={reparsed} matched={matched} '
            f'applied={applied} errors={errors}'))
