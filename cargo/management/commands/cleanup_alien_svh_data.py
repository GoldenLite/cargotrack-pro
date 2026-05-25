"""DEPRECATED — НЕ ИСПОЛЬЗОВАТЬ.

Эта команда была ошибочно создана 2026-05-25 — удаляла warehouse_license/
scan_into_bond/svh_do1_reg_number у Cargo с префиксами 784/555/826/537/880.
Эти данные на самом деле ЛЕГИТИМНЫЕ — их парсит refresh_moscow_cargo
с сайта https://www.moscow-cargo.com/ (см.
cargo/services/external_warehouse/moscow_cargo.py).

Лицензии вида '10005/...' — это лицензии Москва-Карго СВХ, и они
правильно отображаются у партий которые туда едут.

Если эта команда уже была запущена → данные восстанавливаются через
`manage.py refresh_moscow_cargo`.

Команда оставлена только для истории (импорт безопасен) — handle() выкидывает.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = '[УДАЛЕНА] Не использовать. См. docstring модуля.'

    def handle(self, *args, **opts):
        raise CommandError(
            'cleanup_alien_svh_data DEPRECATED. '
            'Москва-Карго данные легитимны (парсятся через refresh_moscow_cargo). '
            'Для восстановления случайно удалённого: '
            'manage.py refresh_moscow_cargo'
        )
