"""Debug-команда для одной партии: тянет данные из «Декларант Плюс» и печатает.

Использование:
    manage.py fetch_deklarant --awb YILI-004           # нормализованный dict
    manage.py fetch_deklarant --awb YILI-004 --raw     # тот же + raw запрос
    manage.py fetch_deklarant --awb YILI-004 --apply   # применить к Cargo если найдено
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from cargo.models import Cargo, DeklarantSession
from cargo.services.external_warehouse import deklarant as deklarant_mod
from cargo.services.external_warehouse.deklarant import (
    DeklarantClient, DeklarantAuthError, DeklarantError,
)


class Command(BaseCommand):
    help = 'Debug: запрос в Декларант Плюс по одному AWB/коносаменту.'

    def add_arguments(self, parser):
        parser.add_argument('--awb', required=True,
                            help='AWB / коносамент для поиска (например YILI-004).')
        parser.add_argument('--raw', action='store_true',
                            help='Показать сырой ответ API (до фильтра WHInn).')
        parser.add_argument('--apply', action='store_true',
                            help='Если нашли — применить к Cargo(awb_number=AWB) через apply_to_cargo.')

    def handle(self, *args, **opts):
        awb = opts['awb'].strip()
        show_raw = bool(opts['raw'])
        do_apply = bool(opts['apply'])

        client = DeklarantClient.from_db()
        if not client:
            raise CommandError(
                'Нет активной DeklarantSession в БД. '
                'Выполни `manage.py deklarant_login` сначала.')

        try:
            with client:
                if not client.session_ok():
                    s = DeklarantSession.get_active()
                    if s:
                        s.mark_dead('session_ok() returned False (likely 401/expired)')
                    raise CommandError('session_ok() == False. Нужен новый QR-логин.')

                self.stdout.write(f'wh_base: {client._get_wh_address()}')
                self.stdout.write(f'region:  {client._region}')
                self.stdout.write(f'target WHInn: {client._target_wh_inn}')
                self.stdout.write('')

                if show_raw:
                    # Делаем сырой запрос (вне фильтра WHInn) для визуальной инспекции
                    import requests
                    url = (f'{client._get_wh_address()}/api/WH/GetInfoByDocumentNumber'
                           f'?region={client._region}')
                    r = client._http.post(url, json={'pattern': awb, 'Stype': 'ExactMatch'},
                                          timeout=30, verify=client._verify_ssl)
                    self.stdout.write(f'HTTP {r.status_code}')
                    try:
                        raw_items = r.json()
                    except Exception:
                        raw_items = r.text
                    self.stdout.write('--- RAW response ---')
                    self.stdout.write(json.dumps(raw_items, ensure_ascii=False, indent=2)[:4000])
                    self.stdout.write('--------------------\n')

                parsed = client.fetch(awb)
        except DeklarantAuthError as e:
            s = DeklarantSession.get_active()
            if s:
                s.mark_dead(f'fetch DeklarantAuthError: {e}')
            raise CommandError(f'DeklarantAuthError: {e}. Сессия помечена как мёртвая.')
        except DeklarantError as e:
            raise CommandError(f'DeklarantError: {e}')

        if not parsed:
            self.stdout.write(self.style.WARNING(
                f'AWB {awb}: ничего не нашли на нашем складе (WHInn={deklarant_mod.TARGET_WH_INN}).'))
            return

        self.stdout.write(self.style.SUCCESS(f'AWB {awb}: НАЙДЕНО на «{parsed.get("wh_name")}»'))
        self.stdout.write(f'  license:    {parsed.get("license")}')
        self.stdout.write(f'  reg_number: {parsed.get("reg_number")}')
        self.stdout.write(f'  do1_date:   {parsed.get("do1_date")}')
        self.stdout.write(f'  do1_number_internal: {parsed.get("do1_number_internal")}')
        self.stdout.write(f'  wh_inn:     {parsed.get("wh_inn")}')
        self.stdout.write(f'  do2_count:  {parsed.get("do2_count")}')

        if not do_apply:
            return

        cargo = Cargo.objects.filter(awb_number__iexact=awb).first()
        if not cargo:
            self.stdout.write(self.style.WARNING(
                f'  --apply: Cargo(awb_number={awb}) не найдён в БД — пропускаем.'))
            return

        from cargo.services.external_warehouse.applier import apply_to_cargo
        changed = apply_to_cargo(cargo, parsed, writeback=True)
        if changed:
            # Маркируем источник (после apply_to_cargo, чтобы не упасть если apply откатился)
            if cargo.svh_source != 'deklarant':
                cargo.svh_source = 'deklarant'
                cargo.save(update_fields=['svh_source'])
            self.stdout.write(self.style.SUCCESS('  --apply: данные применены, svh_source=deklarant.'))
        else:
            self.stdout.write('  --apply: все поля уже заполнены, ничего не писали.')
