"""Alta-agent: pull queued documents from CargoTrack Pro and drop them
into Alta-GTD's hot-folder. ALSO: read incoming customs messages from
Alta-GTD's inbox folder and push to CargoTrack Pro.

CargoTrack Pro (Django, VPS) <-- HTTPS --> this script <-- FS --> C:\\ALTA\\IN

Two threads in one pythonw process:

  [outbound] poll /api/v1/alta/queue/ → download XML → write to hot-folder
             (this is what was here from day one)

  [inbound]  poll C:\\GTDSERV\\ED\\IN\\*.gz → parse SOAP envelope →
             POST to /api/v1/alta/inbox/  (NEW)
             NB: we do NOT delete the .gz files — that's s3_upload_files_ek5.py's
             job. We only read.

Config: alta_agent.ini next to this file.
Run:    python alta_agent.py
"""
from __future__ import annotations

import configparser
import glob
import gzip
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin

import urllib.error
import urllib.request
import zlib

CONFIG_FILE = Path(__file__).resolve().parent / 'alta_agent.ini'
LOG_FILE    = Path(__file__).resolve().parent / 'alta_agent.log'


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(f'Config not found: {CONFIG_FILE}. Copy alta_agent.ini.example to alta_agent.ini and fill it in.')
    cp = configparser.ConfigParser()
    cp.read(CONFIG_FILE, encoding='utf-8')
    s = cp['agent']
    cfg = {
        'base_url':    s['base_url'].rstrip('/') + '/',
        'token':       s['token'].strip(),
        'hotfolder':   Path(s['hotfolder']),
        'interval':    int(s.get('poll_interval_seconds', '4')),
        'retry_sleep': int(s.get('error_retry_seconds', '30')),
    }
    if not cfg['token']:
        sys.exit('alta_agent.ini: token is empty.')
    if not cfg['hotfolder'].exists():
        sys.exit(f'alta_agent.ini: hotfolder does not exist: {cfg["hotfolder"]}')

    # Опциональная inbox-секция (для второй ветки — чтения C:\GTDSERV\ED\IN)
    inbox = None
    if 'inbox' in cp:
        ib = cp['inbox']
        watch_dir = Path(ib.get('watch_dir', ''))
        token = ib.get('token', '').strip()
        if watch_dir and token:
            # Архив — куда копируем каждую .gz ДО парсинга, чтобы s3_upload_files_ek5
            # своим удалением не лишил нас данных. Default — <script_dir>/inbox_archive.
            archive_dir = ib.get('archive_dir', '').strip()
            archive_dir = Path(archive_dir) if archive_dir else (Path(__file__).resolve().parent / 'inbox_archive')
            inbox = {
                'watch_dir':     watch_dir,
                'token':         token,
                'poll_interval': int(ib.get('poll_interval', '5')),
                'state_db':      Path(__file__).resolve().parent / ib.get('state_db', 'inbox_state.sqlite'),
                'endpoint':      ib.get('endpoint', '/api/v1/alta/inbox/').lstrip('/'),
                # читаем только incoming-файлы; outgoing-копии типа `538134^*.gz` пропускаем
                'name_pattern':  re.compile(ib.get('name_pattern', r'^serveralta\^')),
                'archive_dir':       archive_dir,
                # В норме файлы удаляются сразу после успешного POST. Этот
                # параметр — safety-net для застрявших (постоянно failed POST).
                'archive_keep_days': int(ib.get('archive_keep_days', '7')),
            }
    cfg['inbox'] = inbox

    # Опциональная outbox-секция (наблюдение за исходящими копиями `538134^*.gz`)
    # Используется чтобы построить мост: EnvelopeID Альты ↔ наш Cargo/HAWB.
    outbox = None
    if 'outbox' in cp:
        ob = cp['outbox']
        watch_dir = Path(ob.get('watch_dir', '')) if ob.get('watch_dir') else (inbox['watch_dir'] if inbox else None)
        token = ob.get('token', '').strip() or (inbox['token'] if inbox else '')
        if watch_dir and token:
            outbox = {
                'watch_dir':     watch_dir,
                'token':         token,
                'poll_interval': int(ob.get('poll_interval', '5')),
                'state_db':      Path(__file__).resolve().parent / ob.get('state_db', 'outbox_state.sqlite'),
                'endpoint':      ob.get('endpoint', '/api/v1/alta/outbox/').lstrip('/'),
                # `538134^...gz` — основной декларант,
                # `538134-06^...gz` — суб-декларант (СДЭК-06), кладёт копии
                # в `IN\sent\` (другая папка!). Поэтому regex и рекурсивный
                # сканер ниже.
                'name_pattern':  re.compile(ob.get('name_pattern', r'^538134(-\d+)?\^')),
            }
    cfg['outbox'] = outbox

    # Опциональная svh_outbox-секция (наблюдение за исходящими СВХ
    # копиями ed2svh.exe: do1-*.xml в C:\ALTA\SvhPro\ED2SVH\backup_out).
    # Формат отличается — это plain XML без .gz и без Envelope-обёртки.
    # Mtime файла = момент когда ed2svh.exe отправил ДО-1 в таможню.
    svh_outbox = None
    if 'svh_outbox' in cp:
        sob = cp['svh_outbox']
        watch_dir = Path(sob.get('watch_dir', '')) if sob.get('watch_dir') else None
        token = sob.get('token', '').strip() or (inbox['token'] if inbox else '')
        if watch_dir and token:
            svh_outbox = {
                'watch_dir':     watch_dir,
                'token':         token,
                'poll_interval': int(sob.get('poll_interval', '30')),
                'state_db':      Path(__file__).resolve().parent / sob.get('state_db', 'svh_outbox_state.sqlite'),
                'endpoint':      sob.get('endpoint', '/api/v1/alta/outbox/').lstrip('/'),
            }
    cfg['svh_outbox'] = svh_outbox

    # Опциональная db_reconcile-секция (опрос Postgres БД Альты и докачка
    # пропущенных сообщений). Включается флагом enabled=true. Если psycopg2
    # не установлен — поток сам обнаружит и тихо выйдет.
    if cp.has_section('db_reconcile'):
        sec = cp['db_reconcile']
        cfg.update({
            'db_reconcile_enabled':     sec.getboolean('enabled', fallback=False),
            'reconcile_poll_interval':  sec.getint('poll_interval', fallback=600),
            'reconcile_window_days':    sec.getint('window_days', fallback=2),
            'reconcile_chunk':          sec.getint('chunk', fallback=2000),
            'db_host':                  sec.get('db_host', fallback=''),
            'db_port':                  sec.getint('db_port', fallback=5432),
            'db_name':                  sec.get('db_name', fallback=''),
            'db_user':                  sec.get('db_user', fallback=''),
            'db_password':              sec.get('db_password', fallback=''),
            'reconcile_msg_types':      sec.get('msg_types', fallback=''),
        })
    else:
        cfg['db_reconcile_enabled'] = False

    # Опциональная db_reconcile_svh-секция — опрос MS SQL Альта-СВХ сервера
    # для CMN.13010/13029/13014/13021. Этот сервер живёт на другом IP, чем
    # БД ДТ-сервера, и hot-folder для СВХ-входящих в нашу систему не настроен.
    # Без этого reconcile-потока ДО1-регистрации не попадают в CargoTrack
    # автоматически. Реализация через subprocess+PowerShell (System.Data.
    # SqlClient встроен в Windows .NET), т.к. pip-доступ к PyPI заблокирован
    # корпоративным firewall'ом — pymssql/pyodbc не поставить.
    if cp.has_section('db_reconcile_svh'):
        sec = cp['db_reconcile_svh']
        cfg.update({
            'db_reconcile_svh_enabled': sec.getboolean('enabled', fallback=False),
            'svh_poll_interval':        sec.getint('poll_interval', fallback=600),
            'svh_window_days':          sec.getint('window_days', fallback=7),
            'svh_max_per_cycle':        sec.getint('max_per_cycle', fallback=200),
            'svh_throttle_sec':         sec.getfloat('throttle_sec', fallback=0.2),
            'svh_msg_types':            sec.get('msg_types',
                fallback='CMN.13010,CMN.13029,CMN.13014,CMN.13021'),
            # Креды берёт сам PowerShell-скрипт прямо из ini-файла — здесь
            # их не дублируем чтобы не таскать пароли через Python-памятию.
        })
    else:
        cfg['db_reconcile_svh_enabled'] = False

    return cfg


class FlushingFileHandler(logging.FileHandler):
    """File handler that flushes immediately so the log is always tail-able."""
    def emit(self, record):
        super().emit(record)
        self.flush()


def setup_logging() -> None:
    fh = FlushingFileHandler(LOG_FILE, encoding='utf-8')
    sh = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])


def http_request(method: str, url: str, token: str, *, data: bytes | None = None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('User-Agent', 'CargoTrack-AltaAgent/1.1')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = b''
        try:
            body = e.read() if e.fp else b''
        except Exception:
            pass
        return e.code, body, dict(e.headers) if e.headers else {}


def fetch_pending(cfg: dict) -> list[dict]:
    url = urljoin(cfg['base_url'], 'api/v1/alta/queue/')
    status, body, _ = http_request('GET', url, cfg['token'])
    if status != 200:
        raise RuntimeError(f'GET queue: HTTP {status} {body[:200]!r}')
    return json.loads(body.decode('utf-8'))


def download_file(cfg: dict, item_id: int) -> tuple[bytes, str]:
    url = urljoin(cfg['base_url'], f'api/v1/alta/queue/{item_id}/file/')
    status, body, headers = http_request('GET', url, cfg['token'])
    if status != 200:
        raise RuntimeError(f'GET file {item_id}: HTTP {status}')
    filename = headers.get('X-Alta-Filename') or headers.get('x-alta-filename') or f'item_{item_id}.xml'
    return body, filename


def ack(cfg: dict, item_id: int) -> None:
    url = urljoin(cfg['base_url'], f'api/v1/alta/queue/{item_id}/ack/')
    status, body, _ = http_request('POST', url, cfg['token'], data=b'{}')
    if status != 200:
        raise RuntimeError(f'POST ack {item_id}: HTTP {status} {body[:200]!r}')


def fail(cfg: dict, item_id: int, message: str) -> None:
    url = urljoin(cfg['base_url'], f'api/v1/alta/queue/{item_id}/fail/')
    data = json.dumps({'message': message[:5000]}).encode('utf-8')
    try:
        http_request('POST', url, cfg['token'], data=data)
    except Exception as e:
        logging.warning(f'#{item_id}: fail-report itself errored: {e}')


def process_one(cfg: dict, item: dict) -> None:
    item_id = item.get('id')
    try:
        content, filename = download_file(cfg, item_id)
    except Exception as e:
        logging.error(f'#{item_id} {item.get("filename")}: download error: {e}')
        fail(cfg, item_id, f'download: {e}')
        return

    target = cfg['hotfolder'] / Path(filename).name
    try:
        # Atomic write: tmp + rename so Alta doesn't pick up a partial file.
        tmp = target.with_suffix(target.suffix + '.tmp')
        tmp.write_bytes(content)
        tmp.replace(target)
    except Exception as e:
        logging.error(f'#{item_id} {filename}: write error: {e}')
        fail(cfg, item_id, f'write: {e}')
        return

    try:
        ack(cfg, item_id)
        logging.info(f'#{item_id} {filename}: SENT ({len(content)} bytes)')
    except Exception as e:
        # File is already in the hot-folder; server status didn't update.
        # Next poll will re-download and overwrite (idempotent), then re-ack.
        logging.error(f'#{item_id} {filename}: ack error: {e}')


def loop_once(cfg: dict) -> None:
    """One poll cycle, raising only catastrophic errors."""
    try:
        items = fetch_pending(cfg)
    except Exception as e:
        logging.error(f'fetch_pending: {e}; sleeping {cfg["retry_sleep"]}s')
        time.sleep(cfg['retry_sleep'])
        return

    if items:
        logging.info(f'Got {len(items)} pending item(s).')
        for item in items:
            try:
                process_one(cfg, item)
            except Exception as e:
                # Anything unexpected from process_one shouldn't kill the loop.
                logging.error(f'#{item.get("id")}: unexpected: {e}\n{traceback.format_exc()}')

    time.sleep(cfg['interval'])


# ── INBOX (входящие ЭД-сообщения от таможни) ──────────────────────────────

# Парсер использует regex по тегам с локальными именами, чтобы не вязнуть в
# namespace-перфекционизме (там у Альты ~6 разных префиксов).
_TAG_RE = lambda tag: re.compile(r'<(?:[a-zA-Z][\w-]*:)?' + tag + r'\b[^>]*>([^<]*)</(?:[a-zA-Z][\w-]*:)?' + tag + r'>')


def _xml_field(xml_text: str, tag: str) -> str:
    m = _TAG_RE(tag).search(xml_text)
    return (m.group(1).strip() if m else '')


def _pr_document_number_for(xml_text: str, doc_name_substr: str) -> str:
    """Ищет PrDocumentNumber внутри блока с PrDocumentName, содержащим подстроку.

    Например для CMN.11350 нужен WayBillNumber, который лежит как
    <PrDocumentNumber>10245136417</PrDocumentNumber> рядом с
    <PrDocumentName>Индивидуальная накладная</PrDocumentName>.
    """
    pat = re.compile(
        r'<(?:[a-zA-Z][\w-]*:)?PrDocumentName[^>]*>([^<]*)</(?:[a-zA-Z][\w-]*:)?PrDocumentName>\s*'
        r'<(?:[a-zA-Z][\w-]*:)?PrDocumentNumber[^>]*>([^<]*)</(?:[a-zA-Z][\w-]*:)?PrDocumentNumber>',
        re.S
    )
    for name, number in pat.findall(xml_text):
        if doc_name_substr.lower() in name.lower():
            return number.strip()
    return ''


_DECL_TRIPLE_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?CustomsCode\b[^>]*>([^<]+)</(?:[a-zA-Z][\w-]*:)?CustomsCode>\s*'
    r'<(?:[a-zA-Z][\w-]*:)?RegistrationDate\b[^>]*>([^<]+)</(?:[a-zA-Z][\w-]*:)?RegistrationDate>\s*'
    r'<(?:[a-zA-Z][\w-]*:)?GTDNumber\b[^>]*>([^<]+)</(?:[a-zA-Z][\w-]*:)?GTDNumber>',
    re.S
)


def _pick_effective_decl(xml_text: str) -> tuple[str, str, str]:
    """В CMN.11309 (КДТ-уведомление о выпуске) в одном XML может лежать
    несколько разных ДТ — старая и новая корректировочная. Старый парсер брал
    первое попавшееся <GTDNumber> — попадалась старая.

    Приоритет:
    1. Тройка внутри <goom:GTDoutCustomsMark> (release stamp) — актуальная.
    2. Тройка с самой поздней RegistrationDate (новая КДТ).
    3. Fallback на плоские теги (обычное сообщение с одной ДТ).
    """
    mark_block = re.search(
        r'<(?:[a-zA-Z][\w-]*:)?GTDoutCustomsMark\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?GTDoutCustomsMark>',
        xml_text, re.S
    )
    if mark_block:
        m = _DECL_TRIPLE_RE.search(mark_block.group(1))
        if m:
            return (m.group(1).strip(), m.group(2).strip(), m.group(3).strip())

    triples = _DECL_TRIPLE_RE.findall(xml_text)
    if triples:
        best = max(triples, key=lambda t: t[1].strip())
        return (best[0].strip(), best[1].strip(), best[2].strip())

    return (_xml_field(xml_text, 'CustomsCode'),
            _xml_field(xml_text, 'RegistrationDate'),
            _xml_field(xml_text, 'GTDNumber'))


def _parse_inbox_xml(xml_text: str) -> dict:
    """Выкусывает ключевые поля без полноценного XML-парсинга.

    Возвращает dict пригодный для POST /api/v1/alta/inbox/.
    Если ничего не нашли — пустой dict (envelope_id обязателен — caller проверит).
    """
    # WayBillNumber может лежать в разных местах:
    # - прямой тег <WayBillNumber>
    # - в CMN.11350: внутри IndividualWayBill с PrDocumentName="Индивидуальная накладная"
    waybill = (
        _xml_field(xml_text, 'WayBillNumber')
        or _pr_document_number_for(xml_text, 'Индивидуальная накладная')
    )
    cc, rd, gn = _pick_effective_decl(xml_text)
    out = {
        'envelope_id':        _xml_field(xml_text, 'EnvelopeID'),
        'initial_envelope':   _xml_field(xml_text, 'InitialEnvelopeID'),
        'msg_type':           _xml_field(xml_text, 'MessageType'),
        'prepared_at':        _xml_field(xml_text, 'PreparationDateTime'),
        'waybill_number':     waybill,
        'declaration_number': _xml_field(xml_text, 'DeclarationNumber'),
        'customs_code':       cc,
        'registration_date':  rd,
        'gtd_number':         gn,
        'decision_code':      _xml_field(xml_text, 'DecisionCode'),
        # GoodsShipment_HouseShipment\Design — точный код решения по конкретной
        # ДТ (10/11=выпуск, 40=отзыв, …). Точнее чем DecisionCode для CMN.11350.
        'design_code':        _xml_field(xml_text, 'Design'),
        'reason_code':        _xml_field(xml_text, 'ReasonCode'),
        'reason_text':        _xml_field(xml_text, 'Reason'),
        'resolution_text':    _xml_field(xml_text, 'ResolutionDescription'),
        'ref_document_id':    _xml_field(xml_text, 'RefDocumentID'),
        'result_code':        _xml_field(xml_text, 'ResultCode'),
        'result_description': _xml_field(xml_text, 'ResultDescription'),
    }
    # CMN.13029 (WHDocInventory) — представление в таможню, MAWB + якорь UUID.
    if 'WHDocInventory' in xml_text or 'whdi:' in xml_text:
        out.update(_parse_svh_inventory(xml_text))
    # CMN.13010 (DORegInfo) — регистрация ДО1, дата + рег.номер + RefDocumentID.
    if 'DORegInfo' in xml_text or 'dori:' in xml_text:
        out.update(_parse_svh_do1_reg(xml_text))
    return out


# ─── CMN.13029 (Опись СВХ) ──
# Реальная структура (см. cargo/services/alta/xml_extract.py для деталей):
# лицензия в WarehouseOwner/WarehouseLicense/CertificateNumber,
# дата размещения = RegNumberDoc/RegistrationDate,
# MAWB в GoodsShipment/PrDocumentNumber.

_WAREHOUSE_LICENSE_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?WarehouseLicense\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?WarehouseLicense>',
    re.S
)
_GOODS_SHIPMENT_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?GoodsShipment\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?GoodsShipment>',
    re.S
)
_REG_NUMBER_DOC_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?RegNumberDoc\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?RegNumberDoc>',
    re.S
)


def _normalize_mawb(raw: str) -> str:
    """`222-.40333075` → `222-40333075`. Убирает точки и пробелы."""
    return (raw or '').replace('.', '').replace(' ', '').strip()


def _parse_svh_inventory(xml_text: str) -> dict:
    """CMN.13029 → svh_*-поля. Зеркалит cargo.services.alta.xml_extract."""
    out: dict = {}

    lic = _WAREHOUSE_LICENSE_RE.search(xml_text)
    if lic:
        body = lic.group(1)
        out['svh_warehouse_license']  = _xml_field(body, 'CertificateNumber')
        out['svh_warehouse_lic_date'] = _xml_field(body, 'CertificateDate')
        out['svh_warehouse_lic_kind'] = _xml_field(body, 'CertificateKind')

    goods = _GOODS_SHIPMENT_RE.search(xml_text)
    if goods:
        body = goods.group(1)
        raw = _xml_field(body, 'PrDocumentNumber')
        out['svh_mawb_raw'] = raw
        out['svh_mawb']     = _normalize_mawb(raw)
        out['svh_pr_document_date'] = _xml_field(body, 'PrDocumentDate')
        out['svh_pr_document_mode'] = _xml_field(body, 'PresentedDocumentModeCode')

    reg = _REG_NUMBER_DOC_RE.search(xml_text)
    if reg:
        body = reg.group(1)
        cc = _xml_field(body, 'CustomsCode')
        rd = _xml_field(body, 'RegistrationDate')
        gn = _xml_field(body, 'GTDNumber')
        if cc and rd and gn:
            try:
                y, m, d = rd.split('-')
                rd_short = f'{d}{m}{y[2:]}'
            except ValueError:
                rd_short = rd
            out['svh_presentation_reg_number'] = f'{cc}/{rd_short}/{gn}'
        out['svh_presentation_date'] = rd

    iid = _xml_field(xml_text, 'InventoryInstanceDate')
    if iid:
        out['svh_inventory_instance_date'] = iid

    # Якорь связи с CMN.13010 (он положит ссылку в RefDocumentID).
    doc_id = _xml_field(xml_text, 'DocumentID')
    if doc_id:
        out['svh_document_id'] = doc_id

    return out


# ─── CMN.13010 (Регистрация ДО1) ──
_DOREG_INFO_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?DORegInfo\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?DORegInfo>',
    re.S
)
_REGISTER_NUMBER_REPORT_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?RegisterNumberReport\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?RegisterNumberReport>',
    re.S
)


def _parse_svh_do1_reg(xml_text: str) -> dict:
    """CMN.13010 (DORegInfo) → svh_do1_* поля. Зеркалит xml_extract."""
    out: dict = {}

    lic = _WAREHOUSE_LICENSE_RE.search(xml_text)
    if lic:
        body = lic.group(1)
        out['svh_warehouse_license']  = _xml_field(body, 'CertificateNumber')
        out['svh_warehouse_lic_date'] = _xml_field(body, 'CertificateDate')
        out['svh_warehouse_lic_kind'] = _xml_field(body, 'CertificateKind')

    doreg = _DOREG_INFO_RE.search(xml_text)
    if doreg:
        body = doreg.group(1)
        out['svh_do1_reg_date']    = _xml_field(body, 'RegDate')
        out['svh_do1_reg_time']    = _xml_field(body, 'RegTime')
        out['svh_do1_form_report'] = _xml_field(body, 'FormReport')
        out['svh_ref_document_id'] = _xml_field(body, 'RefDocumentID')

        rnr = _REGISTER_NUMBER_REPORT_RE.search(body)
        if rnr:
            rb = rnr.group(1)
            cc = _xml_field(rb, 'CustomsCode')
            rd = _xml_field(rb, 'RegistrationDate')
            gn = _xml_field(rb, 'GTDNumber')
            if cc and rd and gn:
                try:
                    y, m, d = rd.split('-')
                    rd_short = f'{d}{m}{y[2:]}'
                except ValueError:
                    rd_short = rd
                out['svh_do1_reg_number'] = f'{cc}/{rd_short}/{gn}'

    return out


def _state_db_open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE IF NOT EXISTS processed (envelope_id TEXT PRIMARY KEY, processed_at INTEGER)')
    conn.commit()
    return conn


def _state_seen(conn: sqlite3.Connection, envelope_id: str) -> bool:
    cur = conn.execute('SELECT 1 FROM processed WHERE envelope_id=?', (envelope_id,))
    return cur.fetchone() is not None


def _state_mark(conn: sqlite3.Connection, envelope_id: str) -> None:
    conn.execute('INSERT OR IGNORE INTO processed VALUES (?, ?)', (envelope_id, int(time.time())))
    conn.commit()


def _post_inbox(cfg: dict, payload: dict) -> tuple[int, bytes]:
    url = urljoin(cfg['base_url'], cfg['inbox']['endpoint'])
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, method='POST', data=data)
    req.add_header('Authorization', f'Bearer {cfg["inbox"]["token"]}')
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', 'CargoTrack-AltaAgent-Inbox/1.0')
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = b''
        try:
            body = e.read() if e.fp else b''
        except Exception:
            pass
        return e.code, body


def _inbox_process_file(cfg: dict, conn: sqlite3.Connection, path: Path) -> None:
    """Обрабатывает один .gz из архива.

    После успешного POST на VPS (HTTP 200/409) файл из архива удаляется —
    raw_xml уже в БД на VPS, держать вторую копию незачем. При ошибках
    POST файл остаётся для ретрая в следующем цикле.

    Race-protect: если файл уже processed в state.sqlite (был залит в
    предыдущей сессии и почему-то не удалился) — удаляем сразу.
    """
    is_in_archive = (path.parent.name == 'inbox_archive')

    try:
        with open(path, 'rb') as f:
            xml_bytes = gzip.decompress(f.read())
    except FileNotFoundError:
        # s3-скрипт уже удалил — норма
        return
    except OSError as e:
        logging.warning(f'inbox: read {path.name}: {e}')
        return

    # Кодировка обычно UTF-8 в SOAP, но в Альтовских xml встречается cp1251 — fallback
    for enc in ('utf-8', 'cp1251'):
        try:
            xml_text = xml_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        logging.warning(f'inbox: {path.name}: undecodable')
        if is_in_archive:
            _unlink_quiet(path)
        return

    parsed = _parse_inbox_xml(xml_text)
    envelope_id = parsed.get('envelope_id')
    if not envelope_id:
        logging.warning(f'inbox: {path.name}: no EnvelopeID; skipping')
        if is_in_archive:
            _unlink_quiet(path)
        return

    if _state_seen(conn, envelope_id):
        # Уже отправляли раньше — архивная копия больше не нужна
        if is_in_archive:
            _unlink_quiet(path)
        return

    payload = {
        'envelope_id':        envelope_id,
        'msg_type':           parsed.get('msg_type', ''),
        'prepared_at':        parsed.get('prepared_at') or None,
        'waybill_number':     parsed.get('waybill_number', ''),
        'declaration_number': parsed.get('declaration_number', ''),
        'raw_xml':            xml_text,
        'parsed_meta': {
            'source_file':        path.name,
            'initial_envelope':   parsed.get('initial_envelope', ''),
            'ref_document_id':    parsed.get('ref_document_id', ''),
            'customs_code':       parsed.get('customs_code', ''),
            'registration_date':  parsed.get('registration_date', ''),
            'gtd_number':         parsed.get('gtd_number', ''),
            'decision_code':      parsed.get('decision_code', ''),
            'design_code':        parsed.get('design_code', ''),
            'reason_code':        parsed.get('reason_code', ''),
            'reason_text':        parsed.get('reason_text', ''),
            'resolution_text':    parsed.get('resolution_text', ''),
            'result_code':        parsed.get('result_code', ''),
            'result_description': parsed.get('result_description', ''),
        },
    }
    status, body = _post_inbox(cfg, payload)
    if status in (200, 409):
        _state_mark(conn, envelope_id)
        logging.info(f'inbox: {path.name} envelope={envelope_id} kind={parsed.get("msg_type")} → HTTP {status}')
        # Успешно залили на VPS — архивная копия больше не нужна, raw_xml в БД
        if is_in_archive:
            _unlink_quiet(path)
    else:
        logging.error(f'inbox: {path.name}: POST HTTP {status} body={body[:200]!r}')
        # При ошибке файл остаётся в архиве — следующий цикл попытается снова


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _inbox_cleanup_archive(archive_dir: Path, keep_days: int) -> None:
    """Safety-net cleanup: удаляет файлы старше keep_days.

    В норме файлы удаляются сразу после успешного POST в _inbox_process_file.
    Этот cleanup ловит только «застрявшие» — те, что не удалось обработать
    (например, постоянно валился HTTP 500) и которые не имеет смысла держать.
    """
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for p in archive_dir.glob('*.gz'):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        logging.info(f'inbox: cleanup removed {removed} stale .gz from archive')


def inbox_loop(cfg: dict) -> None:
    """Бесконечный поток: копирует новые .gz из watch_dir в archive_dir и
    обрабатывает из архива.

    Зачем архив: на рабочем сервере параллельно работает s3_upload_files_ek5.py,
    который удаляет файлы из watch_dir после своей обработки. Если он опередит
    нас — файл пропадёт. Мы первым делом делаем shutil.copy2 в собственный
    каталог, и работаем уже с копией. Это race-safe.
    """
    ic = cfg['inbox']
    archive_dir: Path = ic['archive_dir']
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logging.error(f'inbox: cannot create archive dir {archive_dir}: {e}')
        return
    logging.info(
        f'inbox: started, watching {ic["watch_dir"]} archive={archive_dir} '
        f'pattern={ic["name_pattern"].pattern!r} poll={ic["poll_interval"]}s '
        f'keep_days={ic["archive_keep_days"]}'
    )
    try:
        conn = _state_db_open(ic['state_db'])
    except Exception as e:
        logging.error(f'inbox: cannot open state db {ic["state_db"]}: {e}')
        return

    last_cleanup = 0.0
    while True:
        try:
            # 1. Скопировать новые .gz из watch_dir в archive (race-safe)
            for p_path in glob.glob(os.path.join(str(ic['watch_dir']), '*.gz')):
                p = Path(p_path)
                if not ic['name_pattern'].search(p.name):
                    continue
                target = archive_dir / p.name
                if target.exists():
                    continue
                try:
                    shutil.copy2(str(p), str(target))
                except FileNotFoundError:
                    # s3_upload удалил между glob и copy — не критично
                    pass
                except OSError as e:
                    logging.warning(f'inbox: copy {p.name}: {e}')

            # 2. Обработать всё из archive (state.sqlite пропускает дубли)
            for p_path in glob.glob(os.path.join(str(archive_dir), '*.gz')):
                p = Path(p_path)
                if not ic['name_pattern'].search(p.name):
                    continue
                try:
                    _inbox_process_file(cfg, conn, p)
                except Exception:
                    logging.error(f'inbox: process {p.name}: {traceback.format_exc()}')

            # 3. Cleanup archive раз в час
            now = time.time()
            if now - last_cleanup > 3600:
                _inbox_cleanup_archive(archive_dir, ic['archive_keep_days'])
                last_cleanup = now
        except Exception:
            logging.error(f'inbox: scan crash: {traceback.format_exc()}')
        time.sleep(ic['poll_interval'])


# ── OUTBOX (наблюдение за исходящими копиями Альты) ───────────────────────

_ALL_WAYBILL_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?WayBillNumber\b[^>]*>([^<]+)</(?:[a-zA-Z][\w-]*:)?WayBillNumber>'
)
_ALL_PRDOCNUM_PAIR_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?PrDocumentName[^>]*>([^<]*)</(?:[a-zA-Z][\w-]*:)?PrDocumentName>\s*'
    r'<(?:[a-zA-Z][\w-]*:)?PrDocumentNumber[^>]*>([^<]+)</(?:[a-zA-Z][\w-]*:)?PrDocumentNumber>',
    re.S,
)


def _all_waybill_numbers(xml_text: str) -> list:
    """Все HAWB-номера в XML — для CMN.11023/11349 (одна декларация = N HAWB).

    Ищет в двух местах:
    1. Все <WayBillNumber> теги (могут быть в каждом GoodsShipment/HouseShipment).
    2. Все <PrDocumentNumber> рядом с <PrDocumentName>Индивидуальная накладная.

    Возвращает дедуплицированный список с сохранением порядка появления.
    """
    seen: set = set()
    out: list = []
    for m in _ALL_WAYBILL_RE.finditer(xml_text):
        v = m.group(1).strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    for name, number in _ALL_PRDOCNUM_PAIR_RE.findall(xml_text):
        if 'индивидуальная накладная' in name.lower():
            v = number.strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
    return out


# Подсчёт товарных позиций. См. cargo/services/alta/xml_extract.py для
# подробного описания логики. Дублируем парсер здесь, чтобы агент мог
# считать на месте и не зависеть от наличия raw_xml на сервере.

_GOODS_ITEM_BLOCK_RE = re.compile(
    r'<(?:[\w-]+:)?ESADout_CUGoods\b[^>]*>(.*?)</(?:[\w-]+:)?ESADout_CUGoods>',
    re.S,
)
_GOODS_GROUP_OPEN_RE = re.compile(
    r'<(?:[\w-]+:)?GoodsGroupDescription\b',
)
_GOODS_DESC_OPEN_RE = re.compile(
    r'<(?:[\w-]+:)?GoodsDescription\b',
)
_HOUSE_SHIPMENT_BLOCK_RE = re.compile(
    r'<(?:[\w-]+:)?HouseShipment\b[^>]*>(.*?)</(?:[\w-]+:)?HouseShipment>',
    re.S,
)
_GOODS_ITEM_DETAILS_OPEN_RE = re.compile(
    r'<(?:[\w-]+:)?GoodsItemDetails\b',
)
_HAWB_PAIR_RE = re.compile(
    r'<(?:[\w-]+:)?PrDocumentNumber\b[^>]*>([^<]+)</(?:[\w-]+:)?PrDocumentNumber>'
    r'[\s\S]{0,500}?'
    r'<(?:[\w-]+:)?(?:DocKindCode|PresentedDocumentModeCode)\b[^>]*>'
    r'([^<]+)'
    r'</(?:[\w-]+:)?(?:DocKindCode|PresentedDocumentModeCode)>'
)


def _count_positions_cmn_11023(xml_text: str) -> int:
    """В CMN.11023: каждый ESADout_CUGoods = 1 товар, внутри +N групп
    (GoodsGroupDescription) или +1 если групп нет, но есть GoodsDescription."""
    total = 0
    for m in _GOODS_ITEM_BLOCK_RE.finditer(xml_text):
        body = m.group(1)
        n_groups = len(_GOODS_GROUP_OPEN_RE.findall(body))
        if n_groups > 0:
            total += n_groups
        elif _GOODS_DESC_OPEN_RE.search(body):
            total += 1
    return total


def _count_positions_per_hawb_cmn_11349(xml_text: str) -> dict:
    """В CMN.11349: внутри каждого HouseShipment считаем GoodsItemDetails
    (= позиции). GoodsDescription может быть разбит на части переносами и
    как счётчик не годится."""
    out: dict = {}
    for m in _HOUSE_SHIPMENT_BLOCK_RE.finditer(xml_text):
        body = m.group(1)
        hawb = ''
        for nm, mode in _HAWB_PAIR_RE.findall(body):
            if mode.strip() == '02021':
                hawb = nm.strip()
                break
        if not hawb:
            continue
        n = len(_GOODS_ITEM_DETAILS_OPEN_RE.findall(body))
        out[hawb] = out.get(hawb, 0) + n
    return out


def _parse_outbox_xml(xml_text: str) -> dict:
    """Парсит ключевые поля из исходящей копии (538134^*.gz).

    Нам нужны: envelope_id (UUID для последующего матчинга входящих
    ответов через InitialEnvelopeID) и человеко-читаемые номера накладных
    (CommonWayBillNumber = MAWB, WayBillNumber = HAWB) — чтобы линковать
    к нашим Cargo/HAWB.

    Для CMN.11023/11349 (одна декларация на партию) дополнительно
    выкусываем ВЕСЬ список HAWB этой декларации → hawbs.
    """
    return {
        'envelope_id':           _xml_field(xml_text, 'EnvelopeID'),
        'msg_type':              _xml_field(xml_text, 'MessageType'),
        'prepared_at':           _xml_field(xml_text, 'PreparationDateTime'),
        'common_waybill_number': _xml_field(xml_text, 'CommonWayBillNumber'),
        'waybill_number': (
            _xml_field(xml_text, 'WayBillNumber')
            or _pr_document_number_for(xml_text, 'Индивидуальная накладная')
        ),
        'hawbs':                 _all_waybill_numbers(xml_text),
        'document_number':       _xml_field(xml_text, 'DocumentNumber'),
        'mcd_id':                _xml_field(xml_text, 'MCDId'),
        'arch_id':                _xml_field(xml_text, 'ArchID'),
        'arch_decl_id':           _xml_field(xml_text, 'ArchDeclID'),
    }


def _post_outbox(cfg: dict, payload: dict) -> tuple[int, bytes]:
    url = urljoin(cfg['base_url'], cfg['outbox']['endpoint'])
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, method='POST', data=data)
    req.add_header('Authorization', f'Bearer {cfg["outbox"]["token"]}')
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', 'CargoTrack-AltaAgent-Outbox/1.0')
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = b''
        try:
            body = e.read() if e.fp else b''
        except Exception:
            pass
        return e.code, body


def _outbox_process_file(cfg: dict, conn: sqlite3.Connection, path: Path) -> None:
    """Обрабатывает одну исходящую копию. Не удаляет файл."""
    try:
        with open(path, 'rb') as f:
            xml_bytes = gzip.decompress(f.read())
    except FileNotFoundError:
        return
    except OSError as e:
        logging.warning(f'outbox: read {path.name}: {e}')
        return

    for enc in ('utf-8', 'cp1251'):
        try:
            xml_text = xml_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        logging.warning(f'outbox: {path.name}: undecodable')
        return

    parsed = _parse_outbox_xml(xml_text)
    envelope_id = parsed.get('envelope_id')
    if not envelope_id:
        return  # без envelope_id наблюдать нечего — тихо пропускаем

    if _state_seen(conn, envelope_id):
        return

    parsed_meta = {
        'source_file':     path.name,
        'document_number': parsed.get('document_number', ''),
        'mcd_id':          parsed.get('mcd_id', ''),
        'arch_id':         parsed.get('arch_id', ''),
        'arch_decl_id':    parsed.get('arch_decl_id', ''),
        # Полный список HAWB этой декларации — для CMN.11023/11349
        # сервер итерирует и проставит filed_date КАЖДОЙ накладной.
        'hawbs':           parsed.get('hawbs', []),
    }
    # Для CMN.11023/11349 — считаем количество товарных позиций и сохраняем
    # полный raw_xml (нужен для пересчёта при изменении логики на сервере).
    # Для CMN.11335 (ПТДЭГ) и CMN.11024 (ДТ) — только raw_xml; сервер парсит
    # экспортные поля через cargo.services.alta.xml_extract на месте.
    msg_type = parsed.get('msg_type', '')
    if msg_type == 'CMN.11023':
        parsed_meta['goods_count'] = _count_positions_cmn_11023(xml_text)
        parsed_meta['raw_xml'] = xml_text
    elif msg_type == 'CMN.11349':
        parsed_meta['goods_count_per_hawb'] = (
            _count_positions_per_hawb_cmn_11349(xml_text)
        )
        parsed_meta['raw_xml'] = xml_text
    elif msg_type in ('CMN.11335', 'CMN.11024'):
        parsed_meta['raw_xml'] = xml_text

    payload = {
        'envelope_id':           envelope_id,
        'msg_type':              msg_type,
        'prepared_at':           parsed.get('prepared_at') or None,
        'common_waybill_number': parsed.get('common_waybill_number', ''),
        'waybill_number':        parsed.get('waybill_number', ''),
        'parsed_meta':           parsed_meta,
    }
    status, body = _post_outbox(cfg, payload)
    if status in (200, 409):
        _state_mark(conn, envelope_id)
        logging.info(
            f'outbox: {path.name} envelope={envelope_id} '
            f'mt={parsed.get("msg_type")} mawb={parsed.get("common_waybill_number","-")} → HTTP {status}'
        )
    else:
        logging.error(f'outbox: {path.name}: POST HTTP {status} body={body[:200]!r}')


# ─────────────────────────────────────────────────────────────────────────
# SVH outbound: ed2svh.exe backup_out — do1-*.xml plain XML без Envelope
# ─────────────────────────────────────────────────────────────────────────
# `ed2svh.exe` копирует исходящие сообщения в backup_out при включённом
# чекбоксе «Резервная копия» в настройках. Файлы не удаляются.
#
# Формат имени: `do1-<CustomsCode>-<YYYYMMDD>-<Seq7>-<Hash8>.xml`
# Пример:       `do1-10001020-20260524-0000873-EA8C8DC8.xml`
#
# Внутри — `<edcnt:ED_Container>` без Envelope-обёртки, EnvelopeID и
# MessageType отсутствуют. Эти поля проставим сами: envelope_id из имени
# файла (это уникальный sequence от Альты), msg_type='ED.DO1'.
# prepared_at = mtime файла (= момент отправки в таможню).
#
# ca-*.xml (Коммерческий акт о расхождении) пропускаем — юзеру не нужен.


# MAWB лежит в отдельном блоке <do1r:MasterAirWayBill> (БЕЗ PresentedDocumentModeCode),
# а HAWB-ы — в <do1r:TransportDocs> с PresentedDocumentModeCode=02021. Два разных
# источника, не один.
_MASTER_AWB_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?MasterAirWayBill\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?MasterAirWayBill>',
    re.S
)
_TRANSPORT_DOCS_BLOCK_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?TransportDocs\b[^>]*>(.*?)</(?:[a-zA-Z][\w-]*:)?TransportDocs>',
    re.S
)
_PR_DOC_NUMBER_INNER_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?PrDocumentNumber>([^<]+)</(?:[a-zA-Z][\w-]*:)?PrDocumentNumber>'
)
_MODE_CODE_INNER_RE = re.compile(
    r'<(?:[a-zA-Z][\w-]*:)?PresentedDocumentModeCode>(\d+)</(?:[a-zA-Z][\w-]*:)?PresentedDocumentModeCode>'
)


def _parse_svh_outbox_xml(xml_text: str) -> dict:
    """Парсит do1-*.xml: ReportNumber + MAWB + список HAWB.

    Возвращает dict с полями для отправки в API /api/v1/alta/outbox/.
    """
    out = {
        'report_number': _xml_field(xml_text, 'ReportNumber'),
        'report_date':   _xml_field(xml_text, 'ReportDate'),
        'certificate_number': _xml_field(xml_text, 'CertificateNumber'),
        'mawb':  '',
        'hawbs': [],
    }
    # MAWB — отдельный блок <MasterAirWayBill> без code
    m_awb = _MASTER_AWB_BLOCK_RE.search(xml_text)
    if m_awb:
        num_m = _PR_DOC_NUMBER_INNER_RE.search(m_awb.group(1))
        if num_m:
            out['mawb'] = num_m.group(1).strip()
    # HAWB-ы — TransportDocs с PresentedDocumentModeCode=02021
    for m in _TRANSPORT_DOCS_BLOCK_RE.finditer(xml_text):
        body = m.group(1)
        num_m = _PR_DOC_NUMBER_INNER_RE.search(body)
        mode_m = _MODE_CODE_INNER_RE.search(body)
        if not num_m or not mode_m:
            continue
        num = num_m.group(1).strip()
        mode = mode_m.group(1).strip()
        if num and mode == '02021':
            out['hawbs'].append(num)
    return out


def _svh_outbox_process_file(cfg: dict, conn: sqlite3.Connection, path: Path) -> None:
    """Обрабатывает один do1-*.xml. Не удаляет/не модифицирует файл."""
    # envelope_id = имя файла без расширения (уникально, sequence+hash от Альты).
    envelope_id = path.stem
    if _state_seen(conn, envelope_id):
        return

    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except FileNotFoundError:
        return
    except OSError as e:
        logging.warning(f'svh_outbox: read {path.name}: {e}')
        return

    for enc in ('utf-8', 'cp1251'):
        try:
            xml_text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        logging.warning(f'svh_outbox: {path.name}: undecodable')
        return

    parsed = _parse_svh_outbox_xml(xml_text)
    if not parsed['hawbs'] and not parsed['mawb']:
        # Это не DO1Report (видимо ca-*.xml или что-то ещё) — пропускаем.
        # Помечаем как seen чтобы не разбирать заново.
        _state_mark(conn, envelope_id)
        return

    # prepared_at = mtime файла (= момент когда ed2svh.exe записал backup =
    # момент отправки в таможню). Делаем aware datetime в UTC ISO формате.
    from datetime import datetime, timezone
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

    # raw_xml шлём для серверного re-парсинга через xml_extract.parse_do1_report
    # (block-based, устойчив к дополнительным тегам в MAWB-блоке типа
    # catWH_ru:Avia/FlightNumber которые ломают плоский regex агента).
    payload = {
        'envelope_id':           envelope_id,
        'msg_type':              'ED.DO1',
        'prepared_at':           mtime,
        'common_waybill_number': parsed['mawb'],
        'waybill_number':        '',  # do1 = партия, не одна HAWB
        'parsed_meta': {
            'source_file':        path.name,
            'report_number':      parsed['report_number'],
            'report_date':        parsed['report_date'],
            'certificate_number': parsed['certificate_number'],
            'hawbs':              parsed['hawbs'],
            'raw_xml':            xml_text,
        },
    }
    status, body = _post_outbox(cfg, payload)
    if status in (200, 409):
        _state_mark(conn, envelope_id)
        logging.info(
            f'svh_outbox: {path.name} report={parsed["report_number"]} '
            f'mawb={parsed["mawb"]} hawbs={len(parsed["hawbs"])} → HTTP {status}'
        )
    else:
        logging.error(f'svh_outbox: {path.name}: POST HTTP {status} body={body[:200]!r}')


def svh_outbox_loop(cfg: dict) -> None:
    """Бесконечный поток: сканит backup_out на do1-*.xml, POST'ит на VPS."""
    sc = cfg['svh_outbox']
    logging.info(
        f'svh_outbox: started, watching {sc["watch_dir"]} poll={sc["poll_interval"]}s'
    )
    try:
        conn = _state_db_open(sc['state_db'])
    except Exception as e:
        logging.error(f'svh_outbox: cannot open state db {sc["state_db"]}: {e}')
        return

    # Лезем в общий _post_outbox — у него ключ 'outbox' жёстко прошит. Подменим
    # ссылку через локальное переименование секции на время этого потока.
    cfg_local = dict(cfg)
    cfg_local['outbox'] = sc  # _post_outbox читает cfg['outbox']['endpoint']/['token']

    while True:
        try:
            files = glob.glob(os.path.join(str(sc['watch_dir']), 'do1-*.xml'))
            for p in (Path(f) for f in files):
                try:
                    _svh_outbox_process_file(cfg_local, conn, p)
                except Exception:
                    logging.error(f'svh_outbox: process {p.name}: {traceback.format_exc()}')
        except Exception:
            logging.error(f'svh_outbox: scan crash: {traceback.format_exc()}')
        time.sleep(sc['poll_interval'])


def outbox_loop(cfg: dict) -> None:
    """Бесконечный поток: сканит watch_dir на 538134^*, POST'ит наблюдения."""
    oc = cfg['outbox']
    logging.info(
        f'outbox: started, watching {oc["watch_dir"]} '
        f'pattern={oc["name_pattern"].pattern!r} poll={oc["poll_interval"]}s'
    )
    try:
        conn = _state_db_open(oc['state_db'])
    except Exception as e:
        logging.error(f'outbox: cannot open state db {oc["state_db"]}: {e}')
        return

    while True:
        try:
            # Сканируем корень IN\ + IN\sent\ — Альта-ГТД для разных
            # декларантов кладёт копии в разные места. `538134^...gz`
            # обычно в корне, `538134-06^...gz` — в sent\.
            files = (
                glob.glob(os.path.join(str(oc['watch_dir']), '*.gz')) +
                glob.glob(os.path.join(str(oc['watch_dir']), 'sent', '*.gz'))
            )
            picked = [Path(p) for p in files if oc['name_pattern'].search(os.path.basename(p))]
            for p in picked:
                try:
                    _outbox_process_file(cfg, conn, p)
                except Exception:
                    logging.error(f'outbox: process {p.name}: {traceback.format_exc()}')
        except Exception:
            logging.error(f'outbox: scan crash: {traceback.format_exc()}')
        time.sleep(oc['poll_interval'])


# ── DB RECONCILE (опрос Postgres Альты и докачка пропущенных) ─────────────
# Альтовский inbox-watcher (вторая ветка выше) читает только файлы из
# C:\GTDSERV\ED\IN. Если файл там по какой-то причине пропустили (агент
# не работал, s3-скрипт удалил быстрее наc, проблемы с архивом, race) —
# в нашем VPS дырка. Этот поток периодически опрашивает БД Альты-ГТД
# (Postgres `alta.edmsgs`), узнаёт у VPS какие envelope_id у нас отсутствуют,
# и докачивает их тела из `edmsgsxml` → отправляет на /api/v1/alta/inbox/.
#
# Безопасность: read-only сессия, autocommit, soft statement_timeout.
# Креды и enabled-флаг — в [db_reconcile] alta_agent.ini.
# psycopg2 опционален — если не установлен, поток тихо выходит.


def _reconcile_one_cycle(pg: dict, base_url: str, token: str,
                         msg_types: list, window_days: int,
                         chunk: int) -> None:
    """Один цикл сверки. Может бросать — caller ловит."""
    import psycopg2  # noqa: F401 — caller already verified import works
    conn = psycopg2.connect(**pg)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(
                """
                SELECT envelopeid::text AS env, messagetype AS mt
                FROM edmsgs
                WHERE incoming = TRUE
                  AND inoutdatetime > now() - (%s || ' days')::interval
                  AND messagetype = ANY(%s)
                """,
                (str(window_days), msg_types),
            )
            heads = cur.fetchall()
        logging.info(f'db_reconcile: PG heads={len(heads)} in window')

        # POST /api/v1/alta/inbox/missing/ чанками — узнаём чего у нас нет
        missing_envs: list = []
        for i in range(0, len(heads), chunk):
            batch = heads[i:i + chunk]
            envs = [r[0] for r in batch]
            req = urllib.request.Request(
                f'{base_url}/api/v1/alta/inbox/missing/',
                data=json.dumps({'envelope_ids': envs}).encode('utf-8'),
                method='POST',
            )
            req.add_header('Authorization', f'Bearer {token}')
            req.add_header('Content-Type', 'application/json')
            req.add_header('User-Agent', 'CargoTrack-AltaAgent-DBReconcile/1.0')
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    body = resp.read()
                    payload = json.loads(body)
                    missing_envs.extend(payload.get('missing', []))
            except Exception:
                logging.exception(
                    f'db_reconcile: missing-check failed for batch {i}')
                continue

        if not missing_envs:
            logging.info('db_reconcile: 0 missing — fully in sync')
            return
        logging.info(
            f'db_reconcile: {len(missing_envs)} missing → fetching bodies'
        )

        # Тянем тела missing и шлём через стандартный /api/v1/alta/inbox/.
        # _post_inbox читает cfg['inbox']['endpoint']/['token'], так что
        # синтезируем минимальный cfg для него.
        post_cfg = {
            'base_url': base_url.rstrip('/') + '/',
            'inbox': {
                'endpoint': '/api/v1/alta/inbox/',
                'token':    token,
            },
        }
        sent = 0
        with conn.cursor() as cur:
            for env in missing_envs:
                try:
                    cur.execute(
                        """
                        SELECT e.messagetype, e.preparationdatetime,
                               edmx.msg AS body, edmx.zip AS zip_flag
                        FROM edmsgs e
                        JOIN edmsgsxml edmx USING (envelopeid)
                        WHERE e.envelopeid = %s
                        """,
                        (env,),
                    )
                    row = cur.fetchone()
                    if not row:
                        logging.warning(
                            f'db_reconcile: envelope {env} body not found, skip'
                        )
                        continue
                    mt, prep_dt, body, zip_flag = row
                    if zip_flag == 1:
                        xml_bytes = zlib.decompress(bytes(body))
                    else:
                        xml_bytes = bytes(body)
                    xml_text = xml_bytes.decode('utf-8', errors='replace')

                    payload = {
                        'envelope_id': env,
                        'msg_type':    mt,
                        'prepared_at': prep_dt.isoformat() if prep_dt else None,
                        'raw_xml':     xml_text,
                        'parsed_meta': {'source': 'db_reconcile'},
                    }
                    status, resp_body = _post_inbox(post_cfg, payload)
                    if status in (200, 409):
                        sent += 1
                    else:
                        logging.warning(
                            f'db_reconcile: POST {env}: HTTP {status} '
                            f'body={resp_body[:200]!r}'
                        )
                except Exception:
                    logging.exception(f'db_reconcile: send {env} crashed')
        logging.info(
            f'db_reconcile: cycle done, sent={sent}/{len(missing_envs)}'
        )
    finally:
        conn.close()


def db_reconcile_loop(cfg: dict) -> None:
    """Сверщик: опрос Postgres Альты + докачка отсутствующих в CargoTrack.

    Каждые reconcile_poll_interval (default 600s = 10 мин):
    1. SELECT envelopeid,messagetype FROM edmsgs WHERE incoming
       AND inoutdatetime > now() - reconcile_window_days
       AND messagetype IN (фильтр-список из конфига)
    2. POST envelope_ids в CargoTrack /api/v1/alta/inbox/missing/
    3. Для каждого missing — SELECT msg FROM edmsgsxml WHERE envelopeid=X
       → zlib.decompress → POST в /api/v1/alta/inbox/ (как агент делает с .gz).

    Включается флагом cfg['db_reconcile_enabled']=true (по умолчанию false
    чтобы не запускать на машинах без psycopg2 / без доступа к PG).
    Креды в [db_reconcile] секции: host, port, dbname, user, password.

    Безопасность: read-only сессия (SET TRANSACTION READ ONLY),
    autocommit, soft timeout (statement_timeout=60s).
    """
    if not cfg.get('db_reconcile_enabled'):
        logging.info('db_reconcile: disabled in config, skipping')
        return

    try:
        import psycopg2  # noqa: F401
    except ImportError:
        logging.warning(
            'db_reconcile: psycopg2 not installed, skipping. '
            'pip install psycopg2-binary'
        )
        return

    poll = int(cfg.get('reconcile_poll_interval', 600))
    window_days = int(cfg.get('reconcile_window_days', 2))
    msg_types_raw = cfg.get('reconcile_msg_types') or (
        'CMN.11337,CMN.11314,CMN.11350,MY.11003,'
        'ED.11003,ED.11010,CMN.11010,CMN.11001,'
        'CMN.11002,CMN.11309,CMN.11310,ED.11002'
    )
    msg_types = [t.strip() for t in msg_types_raw.split(',') if t.strip()]
    pg = {
        'host':            cfg.get('db_host'),
        'port':            int(cfg.get('db_port', 5432)),
        'dbname':          cfg.get('db_name'),
        'user':            cfg.get('db_user'),
        'password':        cfg.get('db_password'),
        'connect_timeout': 10,
        'options':         '-c statement_timeout=60000',  # 60s
    }
    # base_url в нашем cfg хранится с trailing slash; для прямого склеивания
    # с '/api/v1/alta/inbox/missing/' избавляемся от него.
    base_url = cfg['base_url'].rstrip('/')
    # Токен на /api/v1/alta/inbox/* — это ALTA_INBOX_TOKEN, т.е. cfg['inbox']['token'].
    # Fallback на основной токен агента не подходит (другой scope на VPS).
    inbox_cfg = cfg.get('inbox') or {}
    token = inbox_cfg.get('token') or cfg.get('token', '')
    chunk = int(cfg.get('reconcile_chunk', 2000))

    logging.info(
        f'db_reconcile: started, poll={poll}s window={window_days}d '
        f'types={len(msg_types)} host={pg["host"]}'
    )

    while True:
        try:
            _reconcile_one_cycle(pg, base_url, token, msg_types,
                                 window_days, chunk)
        except Exception:
            logging.exception('db_reconcile: cycle crash')
        time.sleep(poll)


def main() -> None:
    setup_logging()
    cfg = load_config()
    logging.info(f'Start. base_url={cfg["base_url"]} hotfolder={cfg["hotfolder"]} interval={cfg["interval"]}s')

    if cfg.get('inbox'):
        threading.Thread(target=inbox_loop, args=(cfg,), daemon=True, name='inbox-loop').start()
    else:
        logging.info('inbox: secton not configured in alta_agent.ini → inbox loop disabled')

    if cfg.get('outbox'):
        threading.Thread(target=outbox_loop, args=(cfg,), daemon=True, name='outbox-loop').start()
    else:
        logging.info('outbox: section not configured in alta_agent.ini → outbox loop disabled')

    if cfg.get('svh_outbox'):
        threading.Thread(target=svh_outbox_loop, args=(cfg,), daemon=True, name='svh-outbox-loop').start()
    else:
        logging.info('svh_outbox: section not configured in alta_agent.ini → SVH outbox loop disabled')

    if cfg.get('db_reconcile_enabled'):
        threading.Thread(target=db_reconcile_loop, args=(cfg,), daemon=True, name='db_reconcile').start()
    else:
        logging.info('db_reconcile: disabled (or section missing) in alta_agent.ini → reconcile loop disabled')

    if cfg.get('db_reconcile_svh_enabled'):
        try:
            import agent_svh
            threading.Thread(
                target=agent_svh.svh_reconcile_loop,
                args=(cfg, _post_inbox, http_request),
                daemon=True,
                name='svh-reconcile',
            ).start()
            # Второй поток — для ED2WHDocInventory parsed-таблицы. Закрывает
            # gap когда CMN.13010 envelope в ED2Msgs отсутствует (Декларант
            # наполняет parsed-таблицу напрямую).
            threading.Thread(
                target=agent_svh.svh_do1_reconcile_loop,
                args=(cfg, _post_inbox, http_request),
                daemon=True,
                name='svh-do1-reconcile',
            ).start()
        except Exception as e:
            logging.error(f'svh_reconcile: failed to start: {e}\n{traceback.format_exc()}')
    else:
        logging.info('db_reconcile_svh: disabled (or section missing) in alta_agent.ini → SVH reconcile loop disabled')

    # Outer guard: a crash inside the loop must not kill the agent silently.
    while True:
        try:
            loop_once(cfg)
        except KeyboardInterrupt:
            logging.info('Stopped by user.')
            return
        except Exception as e:
            logging.error(f'Top-level loop crash: {e}\n{traceback.format_exc()}')
            time.sleep(cfg['retry_sleep'])


if __name__ == '__main__':
    main()
