"""Ручная установка customs_declaration_number для HAWB или всех HAWB партии.

Когда release-сообщение от таможни не дошло до VPS (потеря в гонке
с s3_upload до установки inbox_archive), но юзер точно знает что
выпуск был — можно проставить ДТ вручную.

После: resync_sheets_declarations подтянет в Sheets.

Запуск:
    # Всем HAWB партии:
    uv run python manage.py manual_set_decl --cargo 222-40333075 --decl 10001020/220526/0018648

    # Конкретные HAWB:
    uv run python manage.py manual_set_decl --hawbs 10267530014 10267039841 --decl 10001020/220526/0018648

    # С автоматической записью в Sheets:
    uv run python manage.py manual_set_decl --cargo 222-40333075 --decl 10001020/220526/0018648 --writeback
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from cargo.models import Cargo, HouseWaybill


class Command(BaseCommand):
    help = 'Вручную проставить customs_declaration_number на HAWB / Cargo'

    def add_arguments(self, parser):
        parser.add_argument('--cargo', default='', help='Cargo.awb_number — всем HAWB партии')
        parser.add_argument('--hawbs', nargs='*', default=[], help='Список HAWB-номеров')
        parser.add_argument('--decl', required=True, help='ДТ-номер для записи')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--writeback', action='store_true',
                            help='Сразу записать в Sheets')

    def handle(self, *args, **opts):
        decl = opts['decl'].strip()
        if not decl:
            self.stdout.write(self.style.ERROR('--decl пуст'))
            return

        targets = []  # list[HouseWaybill]

        if opts['cargo']:
            cargo = Cargo.objects.filter(awb_number__iexact=opts['cargo']).first()
            if not cargo:
                self.stdout.write(self.style.ERROR(f'Cargo {opts["cargo"]} не найдена'))
                return
            targets.extend(cargo.hawbs.all())
            self.stdout.write(f'Cargo {cargo.awb_number}: {len(targets)} HAWB-ов')

        if opts['hawbs']:
            found = list(HouseWaybill.objects.filter(hawb_number__in=opts['hawbs']))
            missing = set(opts['hawbs']) - {h.hawb_number for h in found}
            if missing:
                self.stdout.write(f'  не найдены: {sorted(missing)}')
            targets.extend(found)

        if not targets:
            self.stdout.write(self.style.ERROR('Целей нет'))
            return

        # Уникальные по pk
        seen = set()
        unique = []
        for h in targets:
            if h.pk in seen:
                continue
            seen.add(h.pk)
            unique.append(h)
        targets = unique

        self.stdout.write(f'Будет установлено decl={decl} для {len(targets)} HAWB-ов:')
        same = 0
        diff = 0
        for h in targets:
            current = (h.customs_declaration_number or '').strip()
            if current == decl:
                same += 1
                continue
            if current:
                diff += 1
                self.stdout.write(f'  ⚠ {h.hawb_number}: уже стоит {current!r}, перепишем на {decl!r}')

        self.stdout.write(f'  уже совпадает: {same}, переписать с другого значения: {diff}, '
                          f'будет записано: {len(targets) - same}')

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING('DRY RUN — не записано'))
            return

        # Прямой UPDATE минуя save() — иначе HouseWaybill.save()
        # автостирает поле при отсутствии MAWB / неполных документах.
        pks = [h.pk for h in targets]
        HouseWaybill.objects.filter(pk__in=pks).update(customs_declaration_number=decl)
        self.stdout.write(self.style.SUCCESS(f'Записано {len(targets)} HAWB-ов'))

        if opts['writeback']:
            try:
                from cargo.services.sheets.writeback import write_declaration
            except ImportError:
                self.stdout.write('  (writeback недоступен)')
                return
            ok = 0
            for h in targets:
                h.refresh_from_db(fields=['customs_declaration_number'])
                if write_declaration(h):
                    ok += 1
            self.stdout.write(self.style.SUCCESS(f'Sheets writeback: {ok}/{len(targets)}'))
