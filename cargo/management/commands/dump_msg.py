"""Печатает raw_xml + parsed_meta сообщения по envelope_id.

Ищет в AltaInboxMessage и в AltaOutboxObservation. Печатает максимум 200
строк XML — обычно достаточно для поиска якорей (InitialEnvelopeID,
RefDocumentID, DocumentID и т.д.).

Запуск:
    python manage.py dump_msg <envelope_id> [<envelope_id> ...]
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, AltaOutboxObservation


class Command(BaseCommand):
    help = 'Печатает raw_xml + parsed_meta сообщения по envelope_id'

    def add_arguments(self, parser):
        parser.add_argument('envelope_id', nargs='+')
        parser.add_argument('--full', action='store_true',
                            help='Печатать полный XML без обрезки')

    def handle(self, *args, **opts):
        for env in opts['envelope_id']:
            self.show(env, full=opts['full'])
            self.stdout.write('')

    def show(self, env: str, *, full: bool) -> None:
        self.stdout.write(self.style.NOTICE(
            f'\n{"="*70}\n  envelope_id={env}\n{"="*70}'))

        inbox = AltaInboxMessage.objects.filter(envelope_id=env).first()
        if inbox:
            self.stdout.write(f'Найдено в AltaInboxMessage (pk={inbox.pk})')
            self.stdout.write(f'  msg_type     : {inbox.msg_type}')
            self.stdout.write(f'  msg_kind     : {inbox.msg_kind}')
            self.stdout.write(f'  prepared_at  : {inbox.prepared_at}')
            self.stdout.write(f'  cargo_id     : {inbox.cargo_id}')
            self.stdout.write(f'  hawb_id      : {inbox.hawb_id}')
            self.stdout.write('  parsed_meta:')
            self.stdout.write(json.dumps(inbox.parsed_meta or {},
                                          ensure_ascii=False, indent=2))
            self._print_xml(inbox.raw_xml or '', full=full)
            return

        outbox = AltaOutboxObservation.objects.filter(envelope_id=env).first()
        if outbox:
            self.stdout.write(f'Найдено в AltaOutboxObservation (pk={outbox.pk})')
            self.stdout.write(f'  msg_type     : {outbox.msg_type}')
            self.stdout.write(f'  prepared_at  : {outbox.prepared_at}')
            self.stdout.write(f'  common_waybill_number: {outbox.common_waybill_number!r}')
            self.stdout.write(f'  cargo_id     : {outbox.cargo_id}')
            self.stdout.write('  parsed_meta keys: '
                              + ', '.join((outbox.parsed_meta or {}).keys()))
            raw = (outbox.parsed_meta or {}).get('raw_xml', '')
            self._print_xml(raw, full=full)

            other = {k: v for k, v in (outbox.parsed_meta or {}).items()
                     if k != 'raw_xml'}
            self.stdout.write('  parsed_meta (без raw_xml):')
            self.stdout.write(json.dumps(other, ensure_ascii=False, indent=2,
                                          default=str))
            return

        self.stdout.write(self.style.ERROR(
            f'envelope_id {env} не найден ни в inbox, ни в outbox'))

    def _print_xml(self, raw: str, *, full: bool) -> None:
        if not raw:
            self.stdout.write('  raw_xml: <пусто>')
            return
        lines = raw.splitlines()
        if not full and len(lines) > 200:
            self.stdout.write(f'  raw_xml ({len(lines)} строк, показываю первые 200):')
            for ln in lines[:200]:
                self.stdout.write(f'    {ln}')
            self.stdout.write(f'    ... ещё {len(lines)-200} строк (--full для всего)')
        else:
            self.stdout.write(f'  raw_xml ({len(lines)} строк):')
            for ln in lines:
                self.stdout.write(f'    {ln}')
