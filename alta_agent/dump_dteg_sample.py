"""Диагностика: достать ОБРАЗЕЦ исходящего XML ДТЭГ из БД Альты-ГТД.

Зачем: чтобы CargoTrack сам рисовал per-HAWB реестр (выборочную печать) при
выпуске, нам нужен исходный XML подачи ДТЭГ (там товары/ТН ВЭД/документы/
декларант). Он лежит в edmsgsxml как ИСХОДЯЩЕЕ сообщение (incoming=FALSE) —
через обычный db_reconcile мы его не тянем. Этот скрипт его находит и
сохраняет в файл, который потом присылаем разработчику.

ЗАПУСК на машине с Альтой (там, где стоит агент и есть доступ к 10.129.0.9):
    cd C:\ALTA\IN\alta_agent
    "C:\Program Files\Python314\python.exe" dump_dteg_sample.py 10288419748

где 10288419748 — номер любой НЕДАВНО ВЫПУЩЕННОЙ накладной (индивидуальной).
Читает креды БД из alta_agent.ini [db_reconcile] (рядом). Только чтение
(SET TRANSACTION READ ONLY). Ничего не меняет.

Результат: файл dteg_sample_<waybill>.xml рядом со скриптом — пришлите его.
"""
import configparser
import os
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
INI = os.path.join(HERE, 'alta_agent.ini')

WAYBILL = sys.argv[1] if len(sys.argv) > 1 else ''
if not WAYBILL:
    print('Укажи номер выпущенной накладной: python dump_dteg_sample.py 10288419748')
    sys.exit(1)

cp = configparser.ConfigParser()
cp.read(INI, encoding='utf-8')
if not cp.has_section('db_reconcile'):
    print(f'В {INI} нет секции [db_reconcile] с кредами БД.')
    sys.exit(1)
sec = cp['db_reconcile']
pg = dict(host=sec.get('db_host'), port=sec.getint('db_port', 5432),
          dbname=sec.get('db_name'), user=sec.get('db_user'),
          password=sec.get('db_password'), connect_timeout=10,
          options='-c statement_timeout=120000')
print(f'Подключаюсь к БД Альты {pg["host"]}:{pg["port"]}/{pg["dbname"]} ...')

import psycopg2
conn = psycopg2.connect(**pg)
conn.autocommit = True


def decompress(msg, zip_flag):
    b = bytes(msg)
    return (zlib.decompress(b) if zip_flag == 1 else b).decode('utf-8', 'replace')


with conn.cursor() as cur:
    cur.execute('SET TRANSACTION READ ONLY')

    # ── Часть 1: какие ИСХОДЯЩИЕ типы сообщений есть (за 60 дней) ──
    print('\n=== ИСХОДЯЩИЕ (incoming=FALSE) типы за 60 дней ===')
    cur.execute("""
        SELECT messagetype, count(*)
        FROM edmsgs
        WHERE incoming = FALSE AND inoutdatetime > now() - interval '60 days'
        GROUP BY messagetype ORDER BY count(*) DESC
    """)
    for mt, n in cur.fetchall():
        print(f'  {mt:16} {n}')

    # ── Часть 2: исходящие сообщения, чьё тело содержит эту накладную ──
    print(f'\n=== Ищу исходящие с накладной {WAYBILL} (декомпрессия тел) ===')
    cur.execute("""
        SELECT e.envelopeid::text, e.messagetype, e.inoutdatetime,
               edmx.msg, edmx.zip
        FROM edmsgs e JOIN edmsgsxml edmx USING (envelopeid)
        WHERE e.incoming = FALSE
          AND e.inoutdatetime > now() - interval '90 days'
        ORDER BY e.inoutdatetime DESC
        LIMIT 5000
    """)
    best = None  # (score, env, mt, xml)
    checked = 0
    for env, mt, dt, msg, zflag in cur.fetchall():
        checked += 1
        try:
            xml = decompress(msg, zflag)
        except Exception:
            continue
        if WAYBILL not in xml:
            continue
        has_goods = ('GoodsItemDetails' in xml) or ('GoodsDescription' in xml)
        score = (1 if has_goods else 0, len(xml))
        print(f'  {mt:16} env={env[:8]}… len={len(xml)} goods={has_goods} {dt}')
        if best is None or score > best[0]:
            best = (score, env, mt, xml)
    print(f'(проверено исходящих: {checked})')

    if not best:
        print(f'\nНе нашёл исходящих сообщений с накладной {WAYBILL} за 90 дней.')
        print('Попробуй другую (недавно выпущенную) накладную.')
        sys.exit(2)

    _, env, mt, xml = best
    out = os.path.join(HERE, f'dteg_sample_{WAYBILL}.xml')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(xml)
    print(f'\n✅ ОБРАЗЕЦ сохранён: {out}')
    print(f'   messagetype={mt} len={len(xml)}  ← пришлите этот файл разработчику')
conn.close()
