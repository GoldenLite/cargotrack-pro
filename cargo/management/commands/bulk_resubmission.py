"""Массовая регистрация переподачи для группы HAWB.

Сценарий: одна и та же группа HAWB была подана в ДТ-A → отказ → переподана
в ДТ-B → выпуск. CMN-сообщения от таможни могут не дойти до нашей БД
(юзер ведёт декларации в Sheets вручную). Эта команда создаёт корректные
HawbDeclarationAttempt #1 (REJECTED) и #2 (RELEASED) и проставляет
текущий customs_declaration_number = ДТ-B для каждой HAWB.

Запуск:
    uv run python manage.py bulk_resubmission \
        --rejected-decl 10001020/220526/0018637 \
        --released-decl 10001020/220526/0018648 \
        --release-date 2026-05-22T16:00:00+03:00 \
        --hawbs 10265432879 10265935335 ...
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from cargo.models import HawbDeclarationAttempt, HouseWaybill


class Command(BaseCommand):
    help = 'Массовая регистрация переподачи (отказ → выпуск)'

    def add_arguments(self, parser):
        parser.add_argument('--rejected-decl', required=True,
                            help='Декларация первой подачи (получила отказ)')
        parser.add_argument('--released-decl', required=True,
                            help='Декларация второй подачи (получила выпуск)')
        parser.add_argument('--rejected-date',
                            help='ISO datetime отказа (опц.)')
        parser.add_argument('--release-date',
                            help='ISO datetime выпуска (опц.)')
        parser.add_argument('--filed-date',
                            help='ISO datetime подачи (опц.) общая на обе')
        parser.add_argument('--hawbs', nargs='+', required=True)

    def handle(self, *args, **opts):
        from cargo.services.sheets.writeback import (
            begin_batch_writeback, end_batch_writeback,
            batch_write_declarations_for_hawbs,
            batch_write_release_dates_for_hawbs,
            batch_write_filed_dates_for_hawbs,
            batch_write_attempts_count_for_hawbs,
        )

        rej_decl = opts['rejected_decl']
        rel_decl = opts['released_decl']
        rej_dt = (parse_datetime(opts['rejected_date'])
                  if opts['rejected_date'] else None)
        rel_dt = (parse_datetime(opts['release_date'])
                  if opts['release_date'] else None)
        filed_dt = (parse_datetime(opts['filed_date'])
                    if opts['filed_date'] else None)

        affected: list[HouseWaybill] = []
        for hn in opts['hawbs']:
            h = HouseWaybill.objects.filter(hawb_number=hn).first()
            if not h:
                self.stdout.write(f'  {hn}: НЕТ в БД')
                continue

            # Attempt #1 — REJECTED
            a1, c1 = HawbDeclarationAttempt.objects.get_or_create(
                hawb=h, declaration_number=rej_decl,
                defaults={
                    'attempt_number': 1,
                    'status': 'REJECTED',
                    'filed_date': filed_dt,
                    'rejected_date': rej_dt,
                },
            )
            if not c1:
                upd = {}
                if a1.status != 'REJECTED':
                    upd['status'] = 'REJECTED'
                if rej_dt and not a1.rejected_date:
                    upd['rejected_date'] = rej_dt
                if filed_dt and not a1.filed_date:
                    upd['filed_date'] = filed_dt
                if upd:
                    HawbDeclarationAttempt.objects.filter(pk=a1.pk).update(**upd)

            # Attempt #2 — RELEASED
            a2, c2 = HawbDeclarationAttempt.objects.get_or_create(
                hawb=h, declaration_number=rel_decl,
                defaults={
                    'attempt_number': 2,
                    'status': 'RELEASED',
                    'filed_date': filed_dt,
                    'release_date': rel_dt,
                },
            )
            if not c2:
                upd = {}
                if a2.status != 'RELEASED':
                    upd['status'] = 'RELEASED'
                if rel_dt and not a2.release_date:
                    upd['release_date'] = rel_dt
                if filed_dt and not a2.filed_date:
                    upd['filed_date'] = filed_dt
                if upd:
                    HawbDeclarationAttempt.objects.filter(pk=a2.pk).update(**upd)

            # Обновляем HAWB напрямую (UPDATE минуя save())
            upd_h = {'customs_declaration_number': rel_decl}
            if rel_dt:
                upd_h['release_date'] = rel_dt
            if filed_dt:
                upd_h['filed_date'] = filed_dt
            HouseWaybill.objects.filter(pk=h.pk).update(**upd_h)
            h.refresh_from_db()
            affected.append(h)
            self.stdout.write(
                f'  {hn}: attempts={h.declaration_attempts.count()}  '
                f'current={h.customs_declaration_number}')

        if not affected:
            self.stdout.write(self.style.ERROR('Ни одной HAWB не обработано'))
            return

        # Sheets writeback одним пакетом
        self.stdout.write('\nSheets writeback...')
        begin_batch_writeback()
        try:
            pass
        finally:
            end_batch_writeback()

        batch_write_declarations_for_hawbs(affected)
        batch_write_filed_dates_for_hawbs(affected)
        batch_write_release_dates_for_hawbs(affected)
        batch_write_attempts_count_for_hawbs(affected)
        self.stdout.write(self.style.SUCCESS(
            f'\nГотово: {len(affected)} HAWB'))
