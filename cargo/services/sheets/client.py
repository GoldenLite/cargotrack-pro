"""Клиент Google Sheets API через service account."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials


SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']


class SheetsConfigError(RuntimeError):
    """Когда нет credentials или они невалидны — фича просто отключена, не падаем."""


def _get_credentials_path() -> Path:
    raw = (os.environ.get('GOOGLE_SA_CREDENTIALS_FILE') or '').strip()
    if not raw:
        raise SheetsConfigError(
            'GOOGLE_SA_CREDENTIALS_FILE не задан в .env. '
            'Импорт из Google Sheets отключён.'
        )
    path = Path(raw)
    if not path.is_file():
        raise SheetsConfigError(f'Файл credentials не найден: {path}')
    return path


@lru_cache(maxsize=1)
def get_client() -> gspread.Client:
    """Возвращает gspread-клиент с прогретым OAuth2-токеном.

    Кешируется на процесс — token-refresh делает сам gspread по необходимости.
    """
    creds = Credentials.from_service_account_file(str(_get_credentials_path()), scopes=SCOPES)
    return gspread.authorize(creds)


def open_worksheet(source) -> gspread.Worksheet:
    """Открывает конкретную вкладку spreadsheet'а по SheetSource-конфигу."""
    client = get_client()
    spreadsheet = client.open_by_key(source.spreadsheet_id)
    return spreadsheet.worksheet(source.tab_name)
