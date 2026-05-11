"""Alta-agent: pull queued documents from CargoTrack Pro and drop them
into Alta-GTD's hot-folder.

CargoTrack Pro (Django, VPS) -- HTTPS --> this script -- FS --> C:\\ALTA\\inbox

Runs on the machine where Alta-GTD is installed. From the network's point
of view this is just outbound HTTPS, indistinguishable from browser
traffic. No open ports, no VPN.

Config: alta_agent.ini next to this file.
Run:    python alta_agent.py
"""
from __future__ import annotations

import configparser
import json
import logging
import sys
import time
import traceback
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


def main() -> None:
    setup_logging()
    cfg = load_config()
    logging.info(f'Start. base_url={cfg["base_url"]} hotfolder={cfg["hotfolder"]} interval={cfg["interval"]}s')

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
