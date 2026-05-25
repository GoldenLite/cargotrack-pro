"""Выгружает HAWB+MAWB по ДО-1 поданным нашим Альта-СВХ за указанный день/период.

Читает C:\\ALTA\\SvhPro\\ED2SVH\\backup_out\\do1-*.xml, фильтрует по mtime,
парсит каждый XML, складывает в Excel do1_YYYY-MM-DD.xlsx рядом со скриптом.

Файл do1-XX.xml = одна подача ДО-1 = один MAWB + N HAWB. Один MAWB может
быть подан несколькими ДО-1 (если разбили на части).

Запуск:
    python dump_do1_today.py                    # сегодня
    python dump_do1_today.py --date 2026-05-25  # конкретный день
    python dump_do1_today.py --days 7           # последние 7 дней
    python dump_do1_today.py --all              # все что есть в backup_out

Если openpyxl не установлен — fallback в CSV.
"""
from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', default='',
                        help='Конкретный день YYYY-MM-DD (фильтр по имени файла '
                             'do1-10001020-YYYYMMDD-*). По умолчанию — сегодня.')
    parser.add_argument('--all', action='store_true',
                        help='Игнорировать дату, взять все do1-* в backup_out')
    args = parser.parse_args()

    if args.date:
        try:
            target = datetime.date.fromisoformat(args.date)
        except ValueError:
            print(f'Ошибка: --date должна быть YYYY-MM-DD, не {args.date!r}')
            sys.exit(1)
    else:
        target = datetime.date.today()

    # Файлы без расширения, формат: do1-10001020-YYYYMMDD-NNNNNNN-HEX
    pattern = os.path.join(BACKUP_OUT, 'do1-*')
    files = sorted(glob.glob(pattern))
    print(f'Найдено файлов всего: {len(files)} в {BACKUP_OUT}')

    # Фильтр по дате из имени (надёжнее чем mtime — backup_out может
    # пересортироваться/копироваться). do1-XXXXXX-YYYYMMDD-...
    target_str = target.strftime('%Y%m%d')
    rows: list[tuple[str, str]] = []
    n_matched = 0
    for path in files:
        name = os.path.basename(path)
        # Пропускаем не-do1 файлы (на случай если в папке что-то ещё)
        if not name.startswith('do1-'):
            continue
        if not args.all:
            # do1-10001020-20260525-... → ищем YYYYMMDD в имени
            if target_str not in name:
                continue
        n_matched += 1
        mawb, hawbs = parse_do1(path)
        if not mawb:
            print(f'  WARN: {name} — нет MAWB')
            continue
        if not hawbs:
            print(f'  WARN: {name} ({mawb}) — нет HAWB')
            continue
        for hawb in hawbs:
            rows.append((hawb, mawb))

    label = 'все' if args.all else f'за {target}'
    print(f'{label}: {n_matched} файлов, {len(rows)} пар HAWB+MAWB')

    if not rows:
        print('Нечего записывать')
        return

    # Дедуп — на случай если один HAWB упомянут в нескольких do1
    rows = sorted(set(rows))
    print(f'Уникальных пар: {len(rows)}')

    suffix = 'all' if args.all else target.isoformat()
    out_path = f'do1_{suffix}.xlsx'

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
        out_path = f'do1_{suffix}.csv'
        with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
            import csv
            w = csv.writer(f, delimiter=';')
            w.writerow(['HAWB', 'MAWB'])
            for hawb, mawb in rows:
                w.writerow([hawb, mawb])
        print(f'openpyxl не установлен → CSV: {out_path}')


if __name__ == '__main__':
    main()
