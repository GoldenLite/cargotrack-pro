"""Создать EXPORT HAWB вручную + подвязать к существующим inbox-сообщениям.

Используется для легаси-кейсов где outbox CMN.11024/11335/11349 пришёл
со старым агентом (raw_xml=0) и автомат не смог подтвердить ЭК →
HAWB не создалась. CMN.11337/11001 от таможни в БД уже есть, но без
hawb_id. После ручного add_export_hawb они подвяжутся при следующем
dispatch (или сразу через recompute_declaration по hawb_number).

Запуск:
    uv run python manage.py add_export_hawb 10263552584
    uv run python manage.py add_export_hawb 10263552584 10270831117
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, HouseWaybill
from cargo.services.alta.inbox import dispatch, recompute_declaration


class Command(BaseCommand):
    help = 'Ручное создание EXPORT HAWB по hawb_number'

    def add_arguments(self, parser):
        parser.add_argument('hawb_numbers', nargs='+')
        parser.add_argument(
            '--decl-form', default='', choices=['', 'ПТДЭГ', 'ДТЭГ', 'ДТ'],
            help='Тип декларации (ПТДЭГ/ДТЭГ/ДТ). Полезно для legacy кейсов '
                 'где outbox CMN.11024/11335/11349 без raw_xml — автомат '
                 'тип декларации не определит.')

    def handle(self, *args, **opts):
        for hn in opts['hawb_numbers']:
            hn = hn.strip()
            existing = HouseWaybill.objects.filter(
                hawb_number__iexact=hn).first()
            if existing:
                self.stdout.write(self.style.WARNING(
                    f'  {hn}: уже существует (pk={existing.pk}, '
                    f'shipment_type={existing.shipment_type}) — передиспатч'))
                h = existing
            else:
                try:
                    h = HouseWaybill.objects.create(
                        hawb_number=hn,
                        shipment_type='EXPORT',
                        logistics_status='EXPORT_CUSTOMS',
                    )
                    self.stdout.write(self.style.SUCCESS(
                        f'  {hn}: создан pk={h.pk} (EXPORT_CUSTOMS)'))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'  {hn}: ошибка {e}'))
                    continue

            # Если задан явный тип декларации — проставляем сразу
            if opts['decl_form']:
                HouseWaybill.objects.filter(pk=h.pk).update(
                    declaration_form=opts['decl_form'])
                h.refresh_from_db(fields=['declaration_form'])
                self.stdout.write(
                    f'    declaration_form = {h.declaration_form!r}')

            # 1. Привязать висящие CMN.11337/11001/CMN.11002/CMN.11350 без
            #    hawb_id, у которых raw_xml содержит наш hawb_number.
            unattached = AltaInboxMessage.objects.filter(
                hawb__isnull=True, raw_xml__icontains=hn,
            )
            for m in unattached:
                try:
                    dispatch(m)
                except Exception as e:
                    self.stdout.write(f'    dispatch msg pk={m.pk} failed: {e}')

            # 2. recompute_declaration (если есть CMN.11337/11001 с GTDNumber)
            recompute_declaration(h.mawb, h)
            h.refresh_from_db()
            self.stdout.write(
                f'    decl={h.customs_declaration_number!r}  '
                f'status={h.customs_status!r}  '
                f'declarant={h.declarant_name!r}')

        # 3. Writeback экспортных колонок
        from cargo.services.alta.outbox import _writeback_export_hawbs
        all_h = list(HouseWaybill.objects.filter(
            shipment_type='EXPORT',
            hawb_number__in=opts['hawb_numbers'],
        ))
        if all_h:
            _writeback_export_hawbs(all_h)
            self.stdout.write(self.style.SUCCESS('Writeback готов'))
