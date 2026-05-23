"""Диагностика «почему у HAWB нет release_date».

Берёт список HAWB-номеров (из аргумента или файла) и для каждого показывает:
  - есть ли запись HouseWaybill в БД
  - customs_status / release_date / customs_declaration_number
  - сколько AltaInboxMessage привязано к этому HAWB (FK hawb_id)
  - сколько из них с msg_kind='released' (= CMN.11010/11309/11350 после classify)
  - есть ли «orphan»-сообщения: raw_xml содержит номер HAWB, но FK не выставился
  - timestamp последнего входящего сообщения

Сводка в конце разделяет HAWB на 4 категории:
  ok        — есть released-сообщение и release_date выставлен
  missed    — есть released-сообщение, но release_date пустой (apply упал?)
  orphan    — сообщения есть в raw_xml, но к этому HAWB не привязаны
  no_msg    — никакого следа от inbox вообще (race с s3-скриптом?)

Запуск:
    uv run python manage.py check_release_status \\
        --awb 10269467300 10269067339 ...
    # ── или через файл (по одному номеру на строку):
    uv run python manage.py check_release_status --file hawb_list.txt
    # ── только сводка, без построчного отчёта:
    uv run python manage.py check_release_status --file hawb_list.txt --summary
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from cargo.models import AltaInboxMessage, HouseWaybill


class Command(BaseCommand):
    help = 'Диагностика «нет release_date»: где потерялось сообщение'

    def add_arguments(self, parser):
        parser.add_argument('--awb', nargs='*', default=[],
                            help='Номера HAWB через пробел')
        parser.add_argument('--file', help='Путь к файлу со списком HAWB (по одному на строку)')
        parser.add_argument('--summary', action='store_true',
                            help='Только итоговая сводка, без построчного отчёта')

    def handle(self, *args, **opts):
        hawbs = list(opts['awb'])
        if opts['file']:
            with open(opts['file'], encoding='utf-8') as f:
                hawbs.extend(line.strip() for line in f if line.strip())

        hawbs = [h.strip() for h in hawbs if h.strip()]
        if not hawbs:
            raise CommandError('Список HAWB пуст — используй --awb или --file')

        self.stdout.write(f'Проверяю {len(hawbs)} HAWB:\n')

        stats = {'ok': 0, 'missed': 0, 'orphan': 0, 'no_msg': 0, 'no_hawb': 0}
        details = {'ok': [], 'missed': [], 'orphan': [], 'no_msg': [], 'no_hawb': []}

        for awb in hawbs:
            h = HouseWaybill.objects.filter(hawb_number__iexact=awb).first()
            if not h:
                stats['no_hawb'] += 1
                details['no_hawb'].append(awb)
                if not opts['summary']:
                    self.stdout.write(self.style.WARNING(
                        f'  {awb}: НЕТ В БД'))
                continue

            linked = AltaInboxMessage.objects.filter(hawb=h)
            n_total = linked.count()
            released = linked.filter(msg_kind='released')
            n_released = released.count()

            # Поиск orphan: сообщения с этим HAWB в raw_xml, но без FK
            orphan_qs = AltaInboxMessage.objects.filter(
                raw_xml__icontains=awb, hawb__isnull=True
            )
            n_orphan = orphan_qs.count()

            last_msg = linked.order_by('-received_at').first()
            last_dt = last_msg.received_at.strftime('%d.%m %H:%M') if last_msg else '-'

            # Категоризация
            if h.release_date and n_released:
                cat = 'ok'
            elif n_released and not h.release_date:
                cat = 'missed'
            elif n_orphan:
                cat = 'orphan'
            elif n_total == 0:
                cat = 'no_msg'
            else:
                # Есть привязанные сообщения, но ни одного released и release_date пустой.
                # Скорее всего released-сообщение ещё не пришло.
                cat = 'no_msg'

            stats[cat] += 1
            details[cat].append(awb)

            if not opts['summary']:
                style = {
                    'ok':     self.style.SUCCESS,
                    'missed': self.style.ERROR,
                    'orphan': self.style.ERROR,
                    'no_msg': self.style.WARNING,
                }[cat]
                self.stdout.write(style(
                    f'  {awb} [{cat}]: status={h.customs_status or "—"}, '
                    f'release_date={h.release_date.strftime("%d.%m") if h.release_date else "—"}, '
                    f'inbox={n_total} (released={n_released}, orphan={n_orphan}), '
                    f'last={last_dt}'
                ))

        # ── Сводка ────────────────────────────────────────────
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE('=== СВОДКА ==='))
        self.stdout.write(
            f'  ok        — release_date выставлен, released-сообщение есть:  '
            f'{stats["ok"]}'
        )
        self.stdout.write(self.style.ERROR(
            f'  missed    — released-сообщение есть, но release_date пустой:  '
            f'{stats["missed"]}  ← apply упал, нужно reparse'
        ))
        self.stdout.write(self.style.ERROR(
            f'  orphan    — сообщения в raw_xml есть, но к HAWB не привязаны: '
            f'{stats["orphan"]}  ← matcher не нашёл'
        ))
        self.stdout.write(self.style.WARNING(
            f'  no_msg    — никакого следа от inbox:                          '
            f'{stats["no_msg"]}  ← race с s3-скриптом, либо ещё не выпущено'
        ))
        self.stdout.write(
            f'  no_hawb   — HAWB не существует в БД:                          '
            f'{stats["no_hawb"]}'
        )

        # Если есть проблемные — печатаем номера для удобства
        for cat in ('missed', 'orphan'):
            if details[cat]:
                self.stdout.write('')
                self.stdout.write(self.style.ERROR(f'  --- {cat} HAWB ---'))
                for awb in details[cat]:
                    self.stdout.write(f'    {awb}')
