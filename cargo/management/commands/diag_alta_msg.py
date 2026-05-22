"""Diagnostic: показать всё что знаем о конкретном AltaInboxMessage.

    uv run python manage.py diag_alta_msg 3345
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage
from cargo.services.alta.inbox import (
    _build_declaration_number, classify, MSG_KIND_MAP, DESIGN_CODE_KIND,
    DECISION_CODE_KIND,
)


class Command(BaseCommand):
    help = 'Полный дамп AltaInboxMessage + что classify вернёт сейчас'

    def add_arguments(self, parser):
        parser.add_argument('msg_id', type=int)
        parser.add_argument('--grep', default='',
                            help='Фрагмент для поиска в raw_xml — покажет окно вокруг каждого вхождения')
        parser.add_argument('--raw', action='store_true',
                            help='Вывести raw_xml целиком')

    def handle(self, *args, **opts):
        m = AltaInboxMessage.objects.filter(pk=opts['msg_id']).first()
        if not m:
            self.stdout.write(self.style.ERROR(f'AltaInboxMessage {opts["msg_id"]} не найден'))
            return

        self.stdout.write(self.style.SUCCESS(f'=== AltaInboxMessage #{m.pk} ==='))
        self.stdout.write(f'  envelope_id        = {m.envelope_id!r}')
        self.stdout.write(f'  msg_type           = {m.msg_type!r}')
        self.stdout.write(f'  msg_kind (in DB)   = {m.msg_kind!r}')
        self.stdout.write(f'  prepared_at        = {m.prepared_at}')
        self.stdout.write(f'  received_at        = {m.received_at}')
        self.stdout.write(f'  waybill_raw        = {m.waybill_number_raw!r}')
        self.stdout.write(f'  declaration_number = {m.declaration_number!r}')
        self.stdout.write(f'  hawb_id            = {m.hawb_id}')
        self.stdout.write(f'  cargo_id           = {m.cargo_id}')
        self.stdout.write(f'  status_applied     = {m.status_applied}')

        meta = m.parsed_meta or {}
        self.stdout.write(self.style.SUCCESS('\n=== parsed_meta ==='))
        self.stdout.write(json.dumps(meta, ensure_ascii=False, indent=2))

        self.stdout.write(self.style.SUCCESS('\n=== Что classify вернёт сейчас ==='))
        dsn = meta.get('design_code')
        dc = meta.get('decision_code')
        self.stdout.write(f'  msg_type            = {m.msg_type!r}')
        self.stdout.write(f'  design_code raw     = {dsn!r}  (len={len(dsn) if isinstance(dsn,str) else "—"})')
        self.stdout.write(f'  design_code stripped= {(dsn or "").strip()!r}')
        self.stdout.write(f'  DESIGN_CODE_KIND[?] = {DESIGN_CODE_KIND.get((dsn or "").strip(), "—miss—")!r}')
        self.stdout.write(f'  decision_code raw   = {dc!r}')
        self.stdout.write(f'  DECISION_CODE_KIND  = {DECISION_CODE_KIND.get((dc or "").strip(), "—miss—")!r}')
        self.stdout.write(f'  MSG_KIND_MAP[type]  = {MSG_KIND_MAP.get(m.msg_type, "—miss—")!r}')
        result = classify(m.msg_type, meta)
        self.stdout.write(self.style.WARNING(f'\n  classify() → {result!r}'))

        self.stdout.write(self.style.SUCCESS('\n=== built decl_number ==='))
        self.stdout.write(f'  {_build_declaration_number(meta)!r}')

        raw = m.raw_xml or ''
        grep = (opts.get('grep') or '').strip()
        if grep:
            self.stdout.write(self.style.SUCCESS(f'\n=== raw_xml: окна вокруг {grep!r} ==='))
            idx = 0
            n = 0
            while True:
                pos = raw.find(grep, idx)
                if pos < 0:
                    break
                n += 1
                start = max(0, pos - 200)
                end = min(len(raw), pos + len(grep) + 200)
                self.stdout.write(f'--- match #{n} at offset {pos} ---')
                self.stdout.write(raw[start:end])
                idx = pos + len(grep)
            if n == 0:
                self.stdout.write(f'  {grep!r} НЕ найдено в raw_xml')
            else:
                self.stdout.write(f'\n  всего {n} вхождений')
        elif opts.get('raw'):
            self.stdout.write(self.style.SUCCESS('\n=== raw_xml целиком ==='))
            self.stdout.write(raw)
        else:
            self.stdout.write(self.style.SUCCESS('\n=== raw_xml fragment around Design tag ==='))
            if 'Design' in raw:
                idx = raw.find('Design')
                self.stdout.write(raw[max(0, idx - 80):idx + 200])
            else:
                self.stdout.write('  (нет тега Design в raw_xml)')
                self.stdout.write('  Используй --grep <фрагмент> или --raw чтобы посмотреть содержимое.')
