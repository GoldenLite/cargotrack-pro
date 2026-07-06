"""Импорт XML-файлов от Альта-GTD (Alta_CMN*~*.xml, Alta_MY*~*.xml) в БД
с принудительной привязкой к указанной HAWB.

Используется когда:
- Декларация подавалась через Альту, но наш Alta-агент не успел/не смог
  захватить .gz файлы (TimeoutError, HTTP 500, или Альта-GTD удалила .gz
  раньше чем агент скопировал в архив);
- Юзер вручную выгрузил XML-файлы из Альта-GTD UI и хочет их подтянуть.

Файлы делятся на 2 группы по EDHeader/MessageType:
- outbox (наши исходящие): CMN.11349, CMN.11023, CMN.11335, CMN.11024 →
  создаём AltaOutboxObservation. Эти даём ProcessID-якорь для последующего
  attach входящих ответов от таможни.
- inbox (от таможни): CMN.11337, CMN.11350, CMN.11001, CMN.11002, CMN.11010,
  CMN.11309, MY.11003, ED.11003 и др. → создаём AltaInboxMessage + dispatch().

После прогона: если в HawbCustomsRequest остались записи без hawb_id, но
их envelope совпадает с импортированным MY.11003 — force-bind на указанный
HAWB.

Использование:
    manage.py import_alta_xml_dir --hawb 10269026293 --dir 'C:\\Downloads\\10269026293'
"""
from pathlib import Path
import re
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime


logger = logging.getLogger('cargo.import_alta_xml')


OUTBOX_TYPES = {'CMN.11349', 'CMN.11023', 'CMN.11335', 'CMN.11024',
                'CMN.11339', 'CMN.11025', 'CMN.11026', 'CMN.11140',
                'CMN.11213', 'CMN.11417', 'CMN.11416', 'CMN.11074',
                'CMN.11070', 'ED.11001', 'ED.11004'}


def _extract(xml: str, tag_local: str) -> str:
    """Достаёт текст первого тега `tag_local` без учёта namespace."""
    m = re.search(rf'<(?:[\w-]+:)?{re.escape(tag_local)}\b[^>]*>([^<]*)</'
                  rf'(?:[\w-]+:)?{re.escape(tag_local)}>', xml, re.S)
    return m.group(1).strip() if m else ''


def _classify_xml(xml: str) -> tuple[str, str, str, str]:
    """Возвращает (msg_type, envelope_id, prepared_at_str, initial_env)."""
    msg_type = _extract(xml, 'MessageType') or _extract(xml, 'MessageKind')
    # Альта в MessageKind пишет 'MSG.11003' для MY.11003 — нормализуем.
    if msg_type.startswith('MSG.'):
        msg_type = 'MY.' + msg_type[4:]
    envelope_id = _extract(xml, 'EnvelopeID')
    prepared_at = _extract(xml, 'PreparationDateTime')
    initial_env = _extract(xml, 'InitialEnvelopeID')
    return msg_type, envelope_id, prepared_at, initial_env


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--hawb', required=True,
                            help='HAWB-номер для привязки')
        parser.add_argument('--dir', required=True,
                            help='Директория с .xml файлами')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        from cargo.models import (AltaInboxMessage, AltaOutboxObservation,
                                  HouseWaybill, HawbCustomsRequest)
        from cargo.services.alta.inbox import dispatch
        from cargo.services.alta.xml_extract import parse_raw_xml

        hawb_number = opts['hawb'].strip()
        h = HouseWaybill.objects.filter(hawb_number=hawb_number).first()
        if not h:
            self.stdout.write(self.style.ERROR(
                f'HAWB {hawb_number} не найдена в БД'))
            return

        d = Path(opts['dir'])
        if not d.is_dir():
            self.stdout.write(self.style.ERROR(f'не папка: {d}'))
            return

        files = sorted(d.glob('*.xml'))
        if not files:
            self.stdout.write('нет .xml файлов')
            return

        self.stdout.write(f'HAWB {hawb_number} → id={h.id} (mawb={h.mawb.awb_number if h.mawb else None})')
        self.stdout.write(f'Files: {len(files)}\n')

        # Сортируем: сначала outbox (даёт якорь), потом inbox.
        def _order(p):
            xml = p.read_text(encoding='utf-8', errors='replace')
            mt = _extract(xml, 'MessageType') or _extract(xml, 'MessageKind')
            if mt.startswith('MSG.'):
                mt = 'MY.' + mt[4:]
            return (0 if mt in OUTBOX_TYPES else 1, p.name)
        files = sorted(files, key=_order)

        imported_envelopes: set[str] = set()
        for f in files:
            xml = f.read_text(encoding='utf-8', errors='replace')
            msg_type, envelope_id, prepared_str, initial_env = _classify_xml(xml)
            if not msg_type or not envelope_id:
                self.stdout.write(f'  SKIP {f.name}: msg_type or envelope empty')
                continue

            prepared_at = None
            if prepared_str:
                prepared_at = parse_datetime(prepared_str)

            self.stdout.write(
                f'\n  {f.name}\n    msg_type={msg_type} env={envelope_id}\n'
                f'    prepared={prepared_at} initial_env={initial_env!r}')

            if opts['dry_run']:
                continue

            if msg_type in OUTBOX_TYPES:
                # AltaOutboxObservation: даём raw_xml + parsed_meta.hawbs для
                # ProcessID-матчинга последующих inbox'ов.
                try:
                    parsed = parse_raw_xml(xml)
                except Exception:
                    parsed = {}
                # parse_raw_xml кладёт hawbs для outbox-типов; гарантируем.
                if 'hawbs' not in parsed:
                    parsed['hawbs'] = [hawb_number]
                elif hawb_number not in parsed['hawbs']:
                    parsed['hawbs'] = [hawb_number] + list(parsed['hawbs'])
                parsed['raw_xml'] = xml
                obs, created = AltaOutboxObservation.objects.update_or_create(
                    envelope_id=envelope_id,
                    defaults={
                        'msg_type': msg_type[:32],
                        'prepared_at': prepared_at,
                        'common_waybill_number': (
                            h.mawb.awb_number if h.mawb else '')[:64],
                        'waybill_number': hawb_number[:64],
                        'parsed_meta': parsed,
                        'cargo': h.mawb,
                        'hawb': h,
                    },
                )
                imported_envelopes.add(envelope_id)
                self.stdout.write(
                    f'    → OUTBOX {"created" if created else "updated"} '
                    f'(cargo={h.mawb.awb_number if h.mawb else None}, hawb={h.hawb_number})')

            else:
                # AltaInboxMessage + dispatch
                try:
                    parsed = parse_raw_xml(xml)
                except Exception:
                    parsed = {}
                msg, created = AltaInboxMessage.objects.update_or_create(
                    envelope_id=envelope_id,
                    defaults={
                        'msg_type': msg_type[:32],
                        'waybill_number_raw': hawb_number[:64],
                        'prepared_at': prepared_at,
                        'raw_xml': xml,
                        'parsed_meta': parsed,
                    },
                )
                imported_envelopes.add(envelope_id)
                try:
                    dispatch(msg)
                    msg.refresh_from_db()
                    self.stdout.write(
                        f'    → INBOX {"created" if created else "exists"} '
                        f'kind={msg.msg_kind} hawb_id={msg.hawb_id} '
                        f'cargo_id={msg.cargo_id} applied={msg.status_applied}')
                except Exception as e:
                    self.stdout.write(self.style.WARNING(
                        f'    → dispatch failed: {e}'))

        if opts['dry_run']:
            return

        # Force-bind orphan HawbCustomsRequest на указанный HAWB.
        orphans = HawbCustomsRequest.objects.filter(
            envelope_id__in=imported_envelopes, hawb__isnull=True)
        n_bound = orphans.update(hawb=h)
        if n_bound:
            self.stdout.write(self.style.SUCCESS(
                f'\nforce-bound {n_bound} orphan customs_requests to HAWB {h.hawb_number}'))

        # Force-bind inbox messages без hawb_id на указанный HAWB.
        n_inbox_bound = AltaInboxMessage.objects.filter(
            envelope_id__in=imported_envelopes, hawb__isnull=True
        ).update(hawb=h)
        if n_inbox_bound:
            self.stdout.write(self.style.SUCCESS(
                f'force-bound {n_inbox_bound} inbox msgs to HAWB {h.hawb_number}'))

        # Writeback в Sheets: запросы таможни + ed_status.
        from cargo.services.sheets.writeback import (
            batch_write_customs_requests_for_hawbs,
            batch_write_customs_requests_count_for_hawbs,
            batch_write_ed_status_for_hawbs,
            batch_write_declarations_for_hawbs,
        )
        batch_write_customs_requests_for_hawbs([h])
        batch_write_customs_requests_count_for_hawbs([h])
        batch_write_ed_status_for_hawbs([h])
        batch_write_declarations_for_hawbs([h])
        self.stdout.write(self.style.SUCCESS(
            f'\nWriteback done for HAWB {h.hawb_number}'))
