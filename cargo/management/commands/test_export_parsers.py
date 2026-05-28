"""Прогоняет новые парсеры CMN.11335/11024 на присланных XML-примерах."""
from pathlib import Path

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Test parse_cmn_11335 / parse_cmn_11024 / parse_cmn_11349_meta'

    def handle(self, *args, **opts):
        from cargo.services.alta.xml_extract import (
            parse_cmn_11335, parse_cmn_11024, parse_cmn_11349_meta,
        )

        samples = Path(r'C:\Users\Lenovo\Downloads\для парсера')

        for fname, fn in [
            ('Alta_CMN11335~2026_05_28_13_16_39.xml', parse_cmn_11335),
            ('Alta_CMN11335~2026_05_28_11_44_56.xml', parse_cmn_11335),
            ('Alta_CMN11024~2026_05_27_06_06_38.xml', parse_cmn_11024),
            ('Alta_CMN11024~2026_05_20_07_37_24.xml', parse_cmn_11024),
            ('Alta_CMN11349~2026_05_26_14_51_20.xml', parse_cmn_11349_meta),
        ]:
            p = samples / fname
            if not p.exists():
                self.stdout.write(self.style.WARNING(f'нет файла: {p}'))
                continue
            xml = p.read_text(encoding='utf-8')
            r = fn(xml)
            self.stdout.write(self.style.NOTICE(f'\n=== {fname} ==='))
            for k, v in r.items():
                self.stdout.write(f'  {k}: {v}')
