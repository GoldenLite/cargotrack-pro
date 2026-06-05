"""Backfill HouseWaybill.goods_count из CMN.11010 (TotalGoodsNumber).

Покрывает HAWB у которых:
  - customs_declaration_number заполнен
  - goods_count пуст
  - В inbox есть CMN.11010 с непустым raw_xml

По probe-данным (2026-06-05) на проде это ~56 HAWB. Остальные 12,555
не закроются из этого источника (CMN.11010 не приходил с raw_xml).

По умолчанию dry-run. --apply записывает в БД + Sheets writeback.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

from django.core.management.base import BaseCommand

from cargo.models import AltaInboxMessage, HouseWaybill
from cargo.services.alta.xml_extract import count_positions_cmn_11010


SNAPSHOT_DIR = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(sys.executable), '..', '..')),
    'backups', 'goods_count_snapshots',
)


class Command(BaseCommand):
    help = 'Backfill HouseWaybill.goods_count из CMN.11010 (TotalGoodsNumber)'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Реально записать в БД. По умолчанию dry-run.')
        parser.add_argument('--skip-writeback', action='store_true',
                            help='Не запускать Sheets writeback после.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Ограничить N сообщений (для теста).')

    def handle(self, *args, **opts):
        apply = bool(opts.get('apply'))
        limit = opts.get('limit') or 0

        qs = (AltaInboxMessage.objects
              .filter(msg_type='CMN.11010')
              .exclude(raw_xml__isnull=True)
              .exclude(raw_xml='')
              .order_by('-prepared_at'))
        if limit:
            qs = qs[:limit]

        seen: set[int] = set()
        plan: list[tuple] = []  # (hawb_obj, old, new, msg_id)
        snapshot: list[dict] = []

        # Стримим — raw_xml до 132 KB, не хотим всё в RAM.
        for msg in qs.iterator(chunk_size=50):
            total = count_positions_cmn_11010(msg.raw_xml or '')
            if not total:
                continue

            candidates: list[HouseWaybill] = []
            # 1) основной сматч
            if msg.hawb_id:
                h = HouseWaybill.objects.filter(pk=msg.hawb_id).first()
                if h:
                    candidates.append(h)
            # 2) siblings по той же ДТ в той же партии, упомянутые в raw_xml
            if msg.cargo_id and candidates:
                decl = (candidates[0].customs_declaration_number or '').strip()
                if decl:
                    sibs = HouseWaybill.objects.filter(
                        mawb_id=msg.cargo_id,
                        customs_declaration_number=decl,
                    ).exclude(pk__in=[h.pk for h in candidates])
                    raw = msg.raw_xml or ''
                    for sib in sibs:
                        if sib.hawb_number and sib.hawb_number in raw:
                            candidates.append(sib)

            for h in candidates:
                if h.pk in seen:
                    continue
                seen.add(h.pk)
                if h.goods_count and h.goods_count > 0:
                    # only-missing: уже заполнено, не перетираем
                    continue
                plan.append((h, h.goods_count, total, msg.pk))
                snapshot.append({
                    'hawb_id': h.pk,
                    'hawb_number': h.hawb_number,
                    'decl': h.customs_declaration_number,
                    'old_goods_count': h.goods_count,
                    'new_goods_count': total,
                    'inbox_msg_id': msg.pk,
                })

        self.stdout.write(f'Будут затронуты HAWB: {len(plan)}')
        for h, old, new, mid in plan[:30]:
            self.stdout.write(f'  HAWB {h.hawb_number}: {old!r} → {new}  '
                              f'(msg #{mid}, decl {h.customs_declaration_number})')
        if len(plan) > 30:
            self.stdout.write(f'  ... ещё {len(plan)-30}')

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDRY-RUN. Запусти с --apply чтобы записать.'))
            return

        if not plan:
            self.stdout.write('Нечего обновлять.')
            return

        # Snapshot ДО.
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%dT%H%M%SZ')
        snap_path = os.path.join(SNAPSHOT_DIR, f'goods_count_cmn11010_{ts}.json')
        with open(snap_path, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        self.stdout.write(self.style.SUCCESS(f'\nSnapshot: {snap_path}'))

        # UPDATE (минуя save → не дёргаем clearance-логику).
        touched: list[HouseWaybill] = []
        for h, _old, new, _mid in plan:
            HouseWaybill.objects.filter(pk=h.pk).update(goods_count=new)
            touched.append(h)

        self.stdout.write(self.style.SUCCESS(
            f'Обновлено HAWB: {len(touched)}'))

        # Sheets writeback (синхронно — это CLI).
        if opts.get('skip_writeback') or not touched:
            return
        try:
            from cargo.services.sheets.writeback import (
                batch_write_goods_count_for_hawbs,
            )
            for h in touched:
                h.refresh_from_db(fields=['goods_count'])
            # Чанки чтобы не упереться в API rate-limit, между чанками — пауза.
            CHUNK = 50
            written = 0
            for i in range(0, len(touched), CHUNK):
                chunk = touched[i:i+CHUNK]
                try:
                    n = batch_write_goods_count_for_hawbs(chunk) or 0
                    written += n
                except Exception:
                    self.stderr.write(f'  Sheets writeback chunk {i//CHUNK+1} failed')
                    import traceback
                    self.stderr.write(traceback.format_exc())
                if i + CHUNK < len(touched):
                    time.sleep(2)
            self.stdout.write(self.style.SUCCESS(
                f'Sheets writeback: {written} ячеек обновлено'))
        except Exception:
            import traceback
            self.stderr.write(traceback.format_exc())
