"""Найти AltaInboxMessage по hawb_number (включая raw_xml поиск).

Полезно когда inspect_hawb не показывает сообщения — они могут быть в
БД но без привязанной hawb=FK (если match не нашёл HAWB или сохранились
до создания HAWB).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, AltaOutboxObservation


class Command(BaseCommand):
    help = 'Все AltaInboxMessage/Observation упоминающие HAWB-номер'

    def add_arguments(self, parser):
        parser.add_argument('hawbs', nargs='+')

    def handle(self, *args, **opts):
        for hn in opts['hawbs']:
            self.stdout.write(self.style.NOTICE(f'\n=== {hn} ==='))

            # 1. Inbox с привязкой hawb=FK
            direct = AltaInboxMessage.objects.filter(
                hawb__hawb_number=hn).order_by('prepared_at')
            self.stdout.write(f'\n  Inbox прямые (hawb=FK):  {direct.count()}')
            for m in direct:
                pm = m.parsed_meta or {}
                self.stdout.write(
                    f'    {m.prepared_at:%Y-%m-%d %H:%M:%S} '
                    f'{m.msg_type:12s} kind={m.msg_kind:14s} '
                    f'gtd={pm.get("gtd_number", ""):8s} dc={pm.get("decision_code", ""):3s}')

            # 2. Inbox по raw_xml упоминанию
            indirect = AltaInboxMessage.objects.filter(
                raw_xml__icontains=hn).exclude(
                hawb__hawb_number=hn).order_by('prepared_at')
            self.stdout.write(f'\n  Inbox через raw_xml (без FK): {indirect.count()}')
            for m in indirect[:20]:
                pm = m.parsed_meta or {}
                self.stdout.write(
                    f'    {m.prepared_at:%Y-%m-%d %H:%M:%S} '
                    f'{m.msg_type:12s} kind={m.msg_kind:14s} '
                    f'gtd={pm.get("gtd_number", ""):8s} '
                    f'cargo={m.cargo_id} hawb={m.hawb_id}')
            if indirect.count() > 20:
                self.stdout.write(f'    ... ещё {indirect.count() - 20}')

            # 3. Outbox observations
            obs_all = AltaOutboxObservation.objects.filter(
                msg_type__in=['CMN.11335', 'CMN.11349', 'CMN.11024', 'CMN.11023']
            ).order_by('prepared_at')
            obs_hits = []
            for o in obs_all:
                pm = o.parsed_meta or {}
                if hn in (pm.get('hawbs') or []):
                    obs_hits.append(o)
            self.stdout.write(f'\n  Outbox CMN.11023/24/35/49: {len(obs_hits)}')
            for o in obs_hits:
                pm = o.parsed_meta or {}
                self.stdout.write(
                    f'    {o.prepared_at:%Y-%m-%d %H:%M:%S} '
                    f'{o.msg_type:10s} raw_xml_len={len(pm.get("raw_xml") or "")}')
