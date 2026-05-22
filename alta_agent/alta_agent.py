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
            inbox = {
                'watch_dir':     watch_dir,
                'token':         token,
                'poll_interval': int(ib.get('poll_interval', '5')),
                'state_db':      Path(__file__).resolve().parent / ib.get('state_db', 'inbox_state.sqlite'),
                'endpoint':      ib.get('endpoint', '/api/v1/alta/inbox/').lstrip('/'),
                # читаем только incoming-файлы; outgoing-копии типа `538134^*.gz` пропускаем
                'name_pattern':  re.compile(ib.get('name_pattern', r'^serveralta\^')),
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
                'name_pattern':  re.compile(ob.get('name_pattern', r'^538134\^')),
            }
    cfg['outbox'] = outbox
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
        with urllib.request.urlopen(req, timeout=30) as resp:
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
    return {
        'envelope_id':        _xml_field(xml_text, 'EnvelopeID'),
        'initial_envelope':   _xml_field(xml_text, 'InitialEnvelopeID'),
        'msg_type':           _xml_field(xml_text, 'MessageType'),
        'prepared_at':        _xml_field(xml_text, 'PreparationDateTime'),
        'waybill_number':     waybill,
        'declaration_number': _xml_field(xml_text, 'DeclarationNumber'),
        'customs_code':       _xml_field(xml_text, 'CustomsCode'),
        'registration_date':  _xml_field(xml_text, 'RegistrationDate'),
        'gtd_number':         _xml_field(xml_text, 'GTDNumber'),
        'decision_code':      _xml_field(xml_text, 'DecisionCode'),
        'reason_code':        _xml_field(xml_text, 'ReasonCode'),
        'reason_text':        _xml_field(xml_text, 'Reason'),
        'resolution_text':    _xml_field(xml_text, 'ResolutionDescription'),
        'ref_document_id':    _xml_field(xml_text, 'RefDocumentID'),
        'result_code':        _xml_field(xml_text, 'ResultCode'),
        'result_description': _xml_field(xml_text, 'ResultDescription'),
    }


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
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = b''
        try:
            body = e.read() if e.fp else b''
        except Exception:
            pass
        return e.code, body


def _inbox_process_file(cfg: dict, conn: sqlite3.Connection, path: Path) -> None:
    """Обрабатывает один .gz. Не удаляет файл."""
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
        return

    parsed = _parse_inbox_xml(xml_text)
    envelope_id = parsed.get('envelope_id')
    if not envelope_id:
        logging.warning(f'inbox: {path.name}: no EnvelopeID; skipping')
        return

    if _state_seen(conn, envelope_id):
        return  # уже отправляли

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
    else:
        logging.error(f'inbox: {path.name}: POST HTTP {status} body={body[:200]!r}')


def inbox_loop(cfg: dict) -> None:
    """Бесконечный поток: периодически сканит watch_dir, POST новые файлы."""
    ic = cfg['inbox']
    logging.info(
        f'inbox: started, watching {ic["watch_dir"]} '
        f'pattern={ic["name_pattern"].pattern!r} poll={ic["poll_interval"]}s'
    )
    try:
        conn = _state_db_open(ic['state_db'])
    except Exception as e:
        logging.error(f'inbox: cannot open state db {ic["state_db"]}: {e}')
        return

    while True:
        try:
            files = glob.glob(os.path.join(str(ic['watch_dir']), '*.gz'))
            picked = [Path(p) for p in files if ic['name_pattern'].search(os.path.basename(p))]
            for p in picked:
                try:
                    _inbox_process_file(cfg, conn, p)
                except Exception:
                    logging.error(f'inbox: process {p.name}: {traceback.format_exc()}')
        except Exception:
            logging.error(f'inbox: scan crash: {traceback.format_exc()}')
        time.sleep(ic['poll_interval'])


# ── OUTBOX (наблюдение за исходящими копиями Альты) ───────────────────────

def _parse_outbox_xml(xml_text: str) -> dict:
    """Парсит ключевые поля из исходящей копии (538134^*.gz).

    Нам нужны: envelope_id (UUID для последующего матчинга входящих
    ответов через InitialEnvelopeID) и человеко-читаемые номера накладных
    (CommonWayBillNumber = MAWB, WayBillNumber = HAWB) — чтобы линковать
    к нашим Cargo/HAWB.
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
        with urllib.request.urlopen(req, timeout=30) as resp:
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

    payload = {
        'envelope_id':           envelope_id,
        'msg_type':              parsed.get('msg_type', ''),
        'prepared_at':           parsed.get('prepared_at') or None,
        'common_waybill_number': parsed.get('common_waybill_number', ''),
        'waybill_number':        parsed.get('waybill_number', ''),
        'parsed_meta': {
            'source_file':     path.name,
            'document_number': parsed.get('document_number', ''),
            'mcd_id':          parsed.get('mcd_id', ''),
            'arch_id':         parsed.get('arch_id', ''),
            'arch_decl_id':    parsed.get('arch_decl_id', ''),
        },
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
            files = glob.glob(os.path.join(str(oc['watch_dir']), '*.gz'))
            picked = [Path(p) for p in files if oc['name_pattern'].search(os.path.basename(p))]
            for p in picked:
                try:
                    _outbox_process_file(cfg, conn, p)
                except Exception:
                    logging.error(f'outbox: process {p.name}: {traceback.format_exc()}')
        except Exception:
            logging.error(f'outbox: scan crash: {traceback.format_exc()}')
        time.sleep(oc['poll_interval'])


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
