"""manage.py alta_export_hawb <hawb_number>

Генерирует WayBillExpressIndividual XML для одной HAWB,
валидирует против XSD и печатает в stdout (или сохраняет в файл).

Примеры:
    uv run python manage.py alta_export_hawb HAWB-001
    uv run python manage.py alta_export_hawb HAWB-001 --out out.xml
    uv run python manage.py alta_export_hawb HAWB-001 --no-envelope --no-validate
"""
from __future__ import annotations

import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from lxml import etree

from cargo.models import HouseWaybill
from cargo.services.alta import envelope, validator
from cargo.services.alta.generators import waybill_individual


class Command(BaseCommand):
    help = 'Сгенерировать WayBillExpressIndividual XML для одной HAWB'

    def add_arguments(self, parser):
        parser.add_argument('hawb_number', help='Номер HAWB (HouseWaybill.hawb_number)')
        parser.add_argument('--out', help='Путь для сохранения XML (по умолчанию stdout)')
        parser.add_argument('--no-envelope', action='store_true',
                            help='Не оборачивать в SOAP-Envelope (только тело документа)')
        parser.add_argument('--no-validate', action='store_true',
                            help='Не запускать XSD-валидацию')

    def handle(self, *args, hawb_number, out, no_envelope, no_validate, **opts):
        try:
            hawb = HouseWaybill.objects.select_related('mawb').get(
                hawb_number=hawb_number,
            )
        except HouseWaybill.DoesNotExist:
            raise CommandError(f'HAWB не найдена: {hawb_number}')

        # Параметры перевозчика из переменных окружения (.env).
        # На проде будут заполнены реальными реквизитами CDEK / твоей компании.
        carrier_kwargs = dict(
            carrier_name=os.environ.get('ALTA_CARRIER_NAME', 'ТЕСТ-ПЕРЕВОЗЧИК'),
            carrier_cert_number=os.environ.get('ALTA_CARRIER_CERT', '0000/00'),
            carrier_inn=os.environ.get('ALTA_CARRIER_INN', '7700000000'),
            carrier_okpo=os.environ.get('ALTA_CARRIER_OKPO', ''),
            carrier_legal_city=os.environ.get('ALTA_CARRIER_CITY', 'Москва'),
            carrier_legal_street=os.environ.get('ALTA_CARRIER_STREET', ''),
            carrier_fact_city=os.environ.get('ALTA_CARRIER_CITY', 'Москва'),
            carrier_fact_street=os.environ.get('ALTA_CARRIER_STREET', ''),
        )

        body = waybill_individual.build(hawb, **carrier_kwargs)

        if no_envelope:
            xml_bytes = etree.tostring(
                body, xml_declaration=True, encoding='UTF-8', pretty_print=True, standalone=False,
            )
        else:
            xml_bytes = envelope.wrap(
                body_element=body,
                message_type='ED.1002018',  # WayBillExpressIndividual
                participant_id=os.environ.get('ALTA_PARTICIPANT_ID', '0000000000000'),
                receiver_customs_code=os.environ.get('ALTA_CUSTOMS_CODE', '10005030'),
            )

        if not no_validate:
            try:
                # Валидируем тело против WayBillExpressIndividual.xsd
                body_bytes = etree.tostring(body, xml_declaration=True, encoding='UTF-8', standalone=False)
                validator.validate(body_bytes, 'WayBillExpressIndividual.xsd')
                self.stdout.write(self.style.SUCCESS('✓ XSD-валидация пройдена'))
            except validator.XSDValidationError as e:
                self.stdout.write(self.style.ERROR(str(e)))
                # Не падаем — пишем XML всё равно, чтобы можно было посмотреть глазами
                self.stdout.write(self.style.WARNING('XML сохранён, но НЕ валиден — исправь и попробуй снова'))

        if out:
            Path(out).write_bytes(xml_bytes)
            self.stdout.write(self.style.SUCCESS(f'XML сохранён: {out} ({len(xml_bytes)} байт)'))
        else:
            self.stdout.write(xml_bytes.decode('utf-8'))
