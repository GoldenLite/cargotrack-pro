"""QR-логин в API «Декларант Плюс» с сохранением сессии в БД.

Используется на машине с планшетом (телефон с приложением «Мониторинг ДТ»).
На VPS QR не отсканить — там либо повторить логин через RDP, либо вручную
перенести row DeklarantSession между БД (insert через manage.py shell).

Использование:
    manage.py deklarant_login                  # QR + save в БД
    manage.py deklarant_login --qr-out qr.png  # сохранить QR в указанный файл
    manage.py deklarant_login --wait 600       # ждать дольше скана
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from cargo.services.external_warehouse import deklarant_session


class Command(BaseCommand):
    help = 'QR-логин в Декларант Плюс с сохранением сессии в БД (DeklarantSession).'

    def add_arguments(self, parser):
        parser.add_argument('--qr-out', default='deklarant_qr.png',
                            help='Куда сохранить QR (PNG). Default: deklarant_qr.png')
        parser.add_argument('--wait', type=int, default=240,
                            help='Сколько секунд ждать скан. Default: 240')

    def handle(self, *args, **opts):
        qr_path = opts['qr_out']
        wait = opts['wait']

        self.stdout.write('=== Декларант Плюс — QR-логин ===')
        self.stdout.write(f'MDT base: {deklarant_session._mdt_base()}')

        # 1. Регистрируем токен
        try:
            token, _ = deklarant_session.request_qr_token()
        except Exception as e:
            raise CommandError(f'AddQRToken failed: {e}')
        self.stdout.write(f'Token: {token}')

        # 2. Рисуем QR
        try:
            import qrcode
        except ImportError:
            self.stdout.write(self.style.WARNING(
                'Модуль qrcode не установлен — `uv add qrcode[pil]`. '
                f'Введи токен вручную в приложении: {token}'))
        else:
            qr = qrcode.QRCode(
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10, border=4)
            qr.add_data(token)
            qr.make(fit=True)
            try:
                qr.print_ascii(invert=True)
            except Exception:
                pass
            try:
                img = qr.make_image(fill_color='black', back_color='white')
                abs_path = os.path.abspath(qr_path)
                img.save(abs_path)
                self.stdout.write(f'QR saved: {abs_path}')
                if os.name == 'nt':
                    try:
                        os.startfile(abs_path)  # type: ignore[attr-defined]
                    except Exception:
                        pass
            except Exception as e:
                self.stdout.write(self.style.WARNING(f'Не удалось сохранить PNG: {e}'))

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            '>>> Открой «Мониторинг ДТ» на планшете → Вход по QR → наведи на код.'))
        self.stdout.write(f'>>> Жду до {wait} сек ...')

        def _wait(comment):
            self.stdout.write(f'   ... {comment}')

        content = deklarant_session.poll_qr_token(token, timeout_sec=wait, on_wait=_wait)
        if not content:
            raise CommandError('Не дождались скана QR.')

        login = content.get('Login') or ''
        session_id = content.get('Session') or ''
        is_mobile = bool(content.get('IsMobileUser', True))
        if not session_id:
            raise CommandError(f'CheckQRToken вернул success, но Session пустой: {content}')

        s = deklarant_session.save_session(login=login, session_id=session_id,
                                           is_mobile=is_mobile)
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'✓ Сессия сохранена: id={s.id} login={s.login!r}  '
            f'session={s.session_id[:8]}…  is_mobile={s.is_mobile}'))
        self.stdout.write(
            'Для переноса на прод: повторить deklarant_login на VPS через RDP, '
            'либо вручную INSERT в cargo_deklarantsession (login, session_id, '
            'is_mobile=True, is_active=True).')
