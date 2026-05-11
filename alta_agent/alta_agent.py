"""Alta-агент: забирает документы из CargoTrack Pro и кладёт в hot-folder Альты.

Архитектура:
    CargoTrack Pro (Django, VPS) ────HTTPS──── этот скрипт ──FS──> C:\\ALTA\\inbox

Запускается на той машине, где установлена Альта-ГТД (рабочая VPS / личный
ноут). С точки зрения СБ — обычный исходящий HTTPS-трафик к вашему сервису,
неотличимый от браузерной работы.

Конфиг — alta_agent.ini рядом со скриптом. Запуск:
    python alta_agent.py

Чтобы агент работал в фоне постоянно — добавить в Планировщик задач Windows
как «Запускать при входе пользователя», действие: запустить python с этим
файлом. См. README.md.
"""
from __future__ import annotations

import configparser
import logging
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import urllib.request
import urllib.error

CONFIG_FILE = Path(__file__).resolve().parent / 'alta_agent.ini'
LOG_FILE    = Path(__file__).resolve().parent / 'alta_agent.log'


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(f'Не найден {CONFIG_FILE}. Скопируй alta_agent.ini.example в alta_agent.ini и заполни.')
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
        sys.exit('В alta_agent.ini не задан token.')
    if not cfg['hotfolder'].exists():
        sys.exit(f'Hot-folder не найден: {cfg["hotfolder"]}')
    return cfg


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )


def http_request(method: str, url: str, token: str, *, data: bytes | None = None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('User-Agent', 'CargoTrack-AltaAgent/1.0')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b'', dict(e.headers) if e.headers else {}


def fetch_pending(cfg: dict) -> list[dict]:
    import json
    url = urljoin(cfg['base_url'], 'api/v1/alta/queue/')
    status, body, _ = http_request('GET', url, cfg['token'])
    if status != 200:
        raise RuntimeError(f'GET queue failed: HTTP {status} {body[:200]!r}')
    return json.loads(body.decode('utf-8'))


def download_file(cfg: dict, item_id: int) -> tuple[bytes, str]:
    url = urljoin(cfg['base_url'], f'api/v1/alta/queue/{item_id}/file/')
    status, body, headers = http_request('GET', url, cfg['token'])
    if status != 200:
        raise RuntimeError(f'GET file {item_id} failed: HTTP {status}')
    filename = headers.get('X-Alta-Filename') or headers.get('x-alta-filename') or f'item_{item_id}.xml'
    return body, filename


def ack(cfg: dict, item_id: int) -> None:
    url = urljoin(cfg['base_url'], f'api/v1/alta/queue/{item_id}/ack/')
    status, _, _ = http_request('POST', url, cfg['token'], data=b'{}')
    if status != 200:
        raise RuntimeError(f'POST ack {item_id} failed: HTTP {status}')


def fail(cfg: dict, item_id: int, message: str) -> None:
    import json
    url = urljoin(cfg['base_url'], f'api/v1/alta/queue/{item_id}/fail/')
    data = json.dumps({'message': message[:5000]}).encode('utf-8')
    try:
        http_request('POST', url, cfg['token'], data=data)
    except Exception:
        # если даже fail не уехал — переживём, заберём в следующий цикл
        pass


def process_one(cfg: dict, item: dict) -> None:
    item_id = item['id']
    try:
        content, filename = download_file(cfg, item_id)
    except Exception as e:
        logging.error(f'#{item_id} {item.get("filename")}: download error: {e}')
        fail(cfg, item_id, f'download: {e}')
        return

    # Безопасный путь в hot-folder
    target = cfg['hotfolder'] / Path(filename).name
    try:
        # Атомарная запись: сначала во временный файл, потом rename — иначе
        # Альта может попробовать прочитать файл до завершения записи.
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
        logging.error(f'#{item_id} {filename}: ack error: {e}')
        # файл уже в hot-folder, документ статус на сервере не обновился —
        # СЛЕДУЮЩИЙ цикл попробует положить тот же файл повторно; rename идемпотентен.


def main() -> None:
    setup_logging()
    cfg = load_config()
    logging.info(f'Старт. base_url={cfg["base_url"]} hotfolder={cfg["hotfolder"]} interval={cfg["interval"]}s')

    while True:
        try:
            items = fetch_pending(cfg)
        except Exception as e:
            logging.error(f'fetch_pending error: {e}; засыпаю на {cfg["retry_sleep"]}s')
            time.sleep(cfg['retry_sleep'])
            continue

        for item in items:
            process_one(cfg, item)

        time.sleep(cfg['interval'])


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logging.info('Остановлен пользователем.')
