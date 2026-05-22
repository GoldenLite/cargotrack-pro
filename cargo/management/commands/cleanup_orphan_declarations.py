"""Стирает customs_declaration_number у HAWB-ов, для которых в истории
inbox-сообщений НЕТ ни одного released/withdrawn.

Такие «осиротевшие» ДТ остались от старой логики classify, когда
сообщения с decision_code=10 для разных типов считались released, а
теперь classify их не признаёт.

Безопасность: фильтр по cargo (через raw_xml__icontains + hawb.mawb)
такой же, как в recompute_declaration. Не трогает HAWB-ы у которых
release-сообщение существует в БД.

Запуск:
    uv run python manage.py cleanup_orphan_declarations --dry-run
    uv run python manage.py cleanup_orphan_declarations
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from cargo.models import AltaInboxMessage, HouseWaybill


class Command(BaseCommand):
    help = 'Стирает customs_declaration_number у HAWB без release-сообщения в истории'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--writeback', action='store_true',
                            help='Также записать пустоту в Sheets')

    def handle(self, *args, **opts):
        hawbs = HouseWaybill.objects.exclude(customs_declaration_number='')
        total = hawbs.count()
        self.stdout.write(f'HAWB с непустой ДТ: {total}')

        cleared = []
        for h in hawbs.iterator():
            cond = Q(hawb=h)
            if h.mawb_id and h.hawb_number:
                cond = cond | (Q(raw_xml__icontains=h.hawb_number) & Q(cargo=h.mawb))
            has_release = AltaInboxMessage.objects.filter(
                cond, msg_kind__in=('released', 'withdrawn')
            ).exists()
            if not has_release:
                self.stdout.write(
                    f'  orphan: HAWB {h.hawb_number} (id={h.pk}) '
                    f'decl={h.customs_declaration_number!r}'
                )
                cleared.append(h)

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING(f'\nDRY RUN: {len(cleared)} HAWB-ов было бы очищено'))
            return

        if not cleared:
            self.stdout.write(self.style.SUCCESS('Орфанов нет — ничего не делаем'))
            return

        for h in cleared:
            HouseWaybill.objects.filter(pk=h.pk).update(customs_declaration_number='')

        self.stdout.write(self.style.SUCCESS(f'\nОчищено: {len(cleared)} HAWB-ов'))

        if opts['writeback']:
            try:
                from cargo.services.sheets.writeback import write_declaration
            except ImportError:
                self.stdout.write('  (sheets writeback недоступен)')
                return
            for h in cleared:
                h.refresh_from_db(fields=['customs_declaration_number'])
                try:
                    write_declaration(h)
                except Exception as e:
                    self.stdout.write(f'  writeback {h.hawb_number}: {e}')
