"""Диагностика: ищем детерминированный линк между CMN.13029 и CMN.13010.

Альта-СВХ не кладёт MAWB в CMN.13010, а UUID-цепочки разные. Цель команды —
найти ОБЩИЙ якорь в сырых XML обеих типов: GTDNumber, ReportNumber,
RegistrationGoods-ID, что угодно — чтобы матчер был детерминированным.

Запуск:
    uv run python manage.py debug_svh_link --mawb 220526-2 --do1-reg 5012335
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage


# Все теги формата <ns:Tag>value</ns:Tag>
TAG_RE = re.compile(r'<([a-zA-Z_]+:[a-zA-Z_]+)>([^<]+)</\1>')


def extract_tags(xml: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for tag, value in TAG_RE.findall(xml):
        value = (value or '').strip()
        if not value:
            continue
        out.setdefault(tag, []).append(value)
    return out


class Command(BaseCommand):
    help = 'Поиск общих идентификаторов между CMN.13029 и CMN.13010'

    def add_arguments(self, parser):
        parser.add_argument('--mawb', required=True,
                            help='MAWB партии (для поиска CMN.13029)')
        parser.add_argument('--do1-reg', required=True,
                            help='Кусок номера ДО1 (для поиска CMN.13010)')

    def handle(self, *args, **opts):
        mawb = opts['mawb']
        do1_reg = opts['do1_reg']

        p = AltaInboxMessage.objects.filter(
            msg_type='CMN.13029', parsed_meta__svh_mawb=mawb
        ).first()
        if not p:
            self.stdout.write(self.style.ERROR(f'CMN.13029 для {mawb} не найден'))
            return

        d = AltaInboxMessage.objects.filter(
            msg_type='CMN.13010', raw_xml__contains=do1_reg
        ).first()
        if not d:
            self.stdout.write(self.style.ERROR(f'CMN.13010 содержащий {do1_reg} не найден'))
            return

        self.stdout.write(f'CMN.13029 envelope: {p.envelope_id}')
        self.stdout.write(f'CMN.13010 envelope: {d.envelope_id}')
        self.stdout.write('')

        p_tags = extract_tags(p.raw_xml)
        d_tags = extract_tags(d.raw_xml)

        # ── Сводка тегов CMN.13029 ─────
        self.stdout.write(self.style.NOTICE(f'=== CMN.13029 теги ({len(p_tags)} уникальных) ==='))
        for tag in sorted(p_tags):
            vals = p_tags[tag]
            uniq = sorted(set(vals))
            preview = uniq[:3]
            extra = f' (+{len(uniq) - 3} more)' if len(uniq) > 3 else ''
            self.stdout.write(f'  {tag}: {preview}{extra}')

        self.stdout.write('')
        self.stdout.write(self.style.NOTICE(f'=== CMN.13010 теги ({len(d_tags)} уникальных) ==='))
        for tag in sorted(d_tags):
            vals = d_tags[tag]
            uniq = sorted(set(vals))
            preview = uniq[:3]
            extra = f' (+{len(uniq) - 3} more)' if len(uniq) > 3 else ''
            self.stdout.write(f'  {tag}: {preview}{extra}')

        # ── Общие теги с общими значениями ─────
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=== ОБЩИЕ теги с пересекающимися значениями ==='))
        common_tags = set(p_tags) & set(d_tags)
        found_any = False
        for tag in sorted(common_tags):
            p_vals = set(p_tags[tag])
            d_vals = set(d_tags[tag])
            shared = p_vals & d_vals
            # Игнорируем заведомо общие константы (имена, типы)
            if shared:
                # Отфильтровываем кратко-общие константы (например имена/гражданство)
                interesting = {v for v in shared if len(v) >= 4 and not v.isalpha()}
                if interesting:
                    found_any = True
                    self.stdout.write(self.style.SUCCESS(
                        f'  {tag}: {sorted(interesting)}'
                    ))
        if not found_any:
            self.stdout.write('  (общих числовых/uuid значений нет)')

        # ── Поиск значений CMN.13029 в сыром XML CMN.13010 ─────
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            '=== Все значения CMN.13029 которые встречаются в raw_xml CMN.13010 ==='
        ))
        d_xml = d.raw_xml
        for tag, vals in sorted(p_tags.items()):
            for v in set(vals):
                if len(v) < 6:
                    continue
                if v in d_xml:
                    self.stdout.write(f'  CMN.13029 {tag}={v!r} → найден в CMN.13010')
