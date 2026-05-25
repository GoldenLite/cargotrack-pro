"""Выгружает HAWB+MAWB по всем ДО-1 поданным сегодня нашим Альта-СВХ.

Читает C:\\ALTA\\SvhPro\\ED2SVH\\backup_out\\do1-*.xml, фильтрует по mtime
файла (сегодня от 00:00), парсит каждый XML, складывает в Excel
do1_today_YYYY-MM-DD.xlsx рядом со скриптом.

Файл do1-XX.xml = одна подача ДО-1 = один MAWB + N HAWB. Один MAWB может
быть подан несколькими ДО-1 (если разбили на части).

Запуск на рабочем сервере:
    "C:\\Program Files\\Python314\\python.exe" dump_do1_today.py

Если openpyxl не установлен — fallback в CSV.
"""
from __future__ import annotations

import datetime
import glob
import os
import re
import sys


BACKUP_OUT = r'C:\ALTA\SvhPro\ED2SVH\backup_out'

# MAWB: блок <MasterAirWayBill> → внутри <PrDocumentNumber> с MAWB-номером.
_MAWB_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?MasterAirWayBill\b[^>]*>(.*?)'
    r'</(?:[a-zA-Z][\w-]*:)?MasterAirWayBill>',
    re.S,
)

# TransportDocs блоки — каждый HAWB лежит в TransportDocs c
# PresentedDocumentModeCode=02021.
_TRANSPORT_DOCS_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?TransportDocs\b[^>]*>(.*?)'
    r'</(?:[a-zA-Z][\w-]*:)?TransportDocs>',
    re.S,
)


def _first_tag(text: str, tag: str) -> str:
    m = re.search(
        r'<(?:[a-zA-Z][\w-]*:)?' + tag + r'\b[^>]*>([^<]*)</(?:[a-zA-Z][\w-]*:)?' + tag + r'>',
        text,
    )
    return m.group(1).strip() if m else ''


def parse_do1(path: str) -> tuple[str, list[str]]:
    """Возвращает (mawb, [hawbs])."""
    try:
        with open(path, 'r', encoding='cp1251') as f:
            xml = f.read()
    except UnicodeDecodeError:
        with open(path, 'r', encoding='utf-8') as f:
            xml = f.read()

    mawb = ''
    m = _MAWB_BLOCK_RE.search(xml)
    if m:
        mawb = _first_tag(m.group(1), 'PrDocumentNumber')

    hawbs = []
    for m in _TRANSPORT_DOCS_RE.finditer(xml):
        body = m.group(1)
        mode = _first_tag(body, 'PresentedDocumentModeCode')
        num = _first_tag(body, 'PrDocumentNumber')
        if num and mode == '02021':
            hawbs.append(num)

    return mawb, hawbs


def main():
    today = datetime.date.today()
    midnight = datetime.datetime.combine(today, datetime.time(0, 0)).timestamp()

    pattern = os.path.join(BACKUP_OUT, 'do1-*.xml')
    files = sorted(glob.glob(pattern))
    print(f'Найдено файлов всего: {len(files)} в {BACKUP_OUT}')

    rows: list[tuple[str, str]] = []
    n_today = 0
    for path in files:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < midnight:
            continue
        n_today += 1
        mawb, hawbs = parse_do1(path)
        if not mawb:
            print(f'  WARN: {os.path.basename(path)} — нет MAWB')
            continue
        if not hawbs:
            print(f'  WARN: {os.path.basename(path)} ({mawb}) — нет HAWB')
            continue
        for hawb in hawbs:
            rows.append((hawb, mawb))

    print(f'За сегодня ({today}): {n_today} файлов, {len(rows)} пар HAWB+MAWB')

    if not rows:
        print('Нечего записывать')
        return

    # Дедуп — на случай если один HAWB упомянут в нескольких do1
    rows = sorted(set(rows))
    print(f'Уникальных пар: {len(rows)}')

    out_path = f'do1_today_{today.isoformat()}.xlsx'

    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'ДО1 за сегодня'
        ws.append(['HAWB', 'MAWB'])
        for hawb, mawb in rows:
            ws.append([hawb, mawb])
        ws.column_dimensions['A'].width = 18
        ws.column_dimensions['B'].width = 18
        wb.save(out_path)
        print(f'Записано: {out_path}')
    except ImportError:
        out_path = f'do1_today_{today.isoformat()}.csv'
        with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
            import csv
            w = csv.writer(f, delimiter=';')
            w.writerow(['HAWB', 'MAWB'])
            for hawb, mawb in rows:
                w.writerow([hawb, mawb])
        print(f'openpyxl не установлен → CSV: {out_path}')


if __name__ == '__main__':
    main()
