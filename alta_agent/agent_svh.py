"""SVH reconcile loop for alta_agent.

Pulls СВХ-сообщений (CMN.13010/13029/13014/13021) from Alta-SVH MS SQL
server via PowerShell helper (svh_query.ps1) — no pip dependencies needed
since pip access to pypi.org is blocked from the work network.

Mirror of db_reconcile_loop() for Postgres ДТ-сервер, but for the SVH
MS SQL server which lives at a different IP. Designed to coexist with
the existing pg-reconcile thread.

Usage in alta_agent.py main():

    if cfg.get('db_reconcile_svh_enabled'):
        threading.Thread(
            target=agent_svh.svh_reconcile_loop,
            args=(cfg, _post_inbox, http_request),
            daemon=True,
        ).start()

Config in alta_agent.ini:
    [db_reconcile_svh]
    enabled = true
    poll_interval = 600
    window_days = 7
    max_per_cycle = 200
    throttle_sec = 0.2
    msg_types = CMN.13010,CMN.13029,CMN.13014,CMN.13021
    db_host = 10.129.0.33
    db_port = 1433
    db_name = AltaSVHDb
    db_user = ...
    db_password = ...
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


logger = logging.getLogger('svh_reconcile')

# Path to the PowerShell helper next to this file
_HERE = Path(__file__).resolve().parent
_PS1 = _HERE / 'svh_query.ps1'

# Default timeout for fetch op (review concern #7: not 180)
_FETCH_TIMEOUT = 60   # seconds — batched fetch of up to chunk envelopes
_LIST_TIMEOUT = 60    # seconds — single list query


def _check_pwsh() -> Optional[str]:
    """Pick powershell.exe (PS 5.1, Windows). Don't use pwsh.exe (PS7+) —
    System.Data.SqlClient packaging is different there."""
    for cand in ('powershell.exe',
                 r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'):
        try:
            r = subprocess.run([cand, '-NoProfile', '-Command', '$PSVersionTable.PSVersion.Major'],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                return cand
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def _run_ps(args: list, timeout: int = 60, op_label: str = '') -> tuple[int, list[dict]]:
    """Run svh_query.ps1 with given args, return (exit_code, [jsonl_rows]).
    Non-JSON stdout lines are logged at DEBUG and skipped (review concern #14).
    """
    if not _PS1.exists():
        logger.error('svh_query.ps1 not found at %s — skip cycle', _PS1)
        return (10, [])
    pwsh = _check_pwsh()
    if not pwsh:
        logger.error('Windows PowerShell 5.1 not available; SVH reconcile disabled')
        return (11, [])
    full_args = [pwsh, '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-File', str(_PS1)] + args
    try:
        r = subprocess.run(full_args, capture_output=True, text=True,
                           encoding='utf-8', errors='replace',
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning('svh_query %s timeout (%ds)', op_label, timeout)
        return (12, [])
    rows = []
    for line in (r.stdout or '').splitlines():
        line = line.strip()
        if not line or not line.startswith('{'):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug('svh_query %s non-JSON line: %s', op_label, line[:120])
    if r.returncode != 0:
        logger.warning('svh_query %s exit=%s stderr=%s', op_label,
                       r.returncode, (r.stderr or '')[:500])
    return (r.returncode, rows)


def _post_missing(http_request_fn, base_url: str, token: str,
                  envelope_ids: list[str]) -> list[str]:
    """Call /api/v1/alta/inbox/missing/ — returns envelope_ids absent from DB."""
    url = f'{base_url}/api/v1/alta/inbox/missing/'
    payload = {'envelope_ids': envelope_ids}
    body = json.dumps(payload).encode('utf-8')
    try:
        status, resp, _ = http_request_fn('POST', url, token, data=body)
    except Exception as e:
        logger.warning('missing-check failed: %s', e)
        return []
    if status != 200:
        logger.warning('missing-check HTTP %s: %s', status,
                       (resp or b'')[:200])
        return []
    try:
        data = json.loads(resp.decode('utf-8') if isinstance(resp, bytes) else resp)
        return data.get('missing') or []
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
        return []


def svh_reconcile_one_cycle(cfg: dict, post_inbox_fn, http_request_fn) -> None:
    """One pass: list → missing → fetch (batched) → POST throttled."""
    base_url = cfg['base_url'].rstrip('/')
    token = (cfg.get('inbox', {}) or {}).get('token') or cfg.get('token', '')
    window_days = int(cfg.get('svh_window_days', 7))
    max_per_cycle = int(cfg.get('svh_max_per_cycle', 200))
    throttle = float(cfg.get('svh_throttle_sec', 0.2))
    msg_types = cfg.get('svh_msg_types') or 'CMN.13010,CMN.13029,CMN.13014,CMN.13021'

    # 1. List envelopes in window
    code, rows = _run_ps(
        ['-Op', 'list',
         '-SinceDays', str(window_days),
         '-Types', msg_types],
        timeout=_LIST_TIMEOUT, op_label='list'
    )
    if code != 0:
        return
    all_envs = [r['envelope_id'] for r in rows if r.get('envelope_id')]
    if not all_envs:
        logger.info('svh_reconcile: 0 envelopes in window (%dd) — fully in sync',
                    window_days)
        return

    # 2. Missing-check in chunks (server cap is 5000 per request)
    CHECK_CHUNK = 2000
    missing = []
    for i in range(0, len(all_envs), CHECK_CHUNK):
        sub = all_envs[i:i + CHECK_CHUNK]
        miss = _post_missing(http_request_fn, base_url, token, sub)
        missing.extend(miss)

    if not missing:
        logger.info('svh_reconcile: PG heads=%d in window, 0 missing — in sync',
                    len(all_envs))
        return

    logger.info('svh_reconcile: heads=%d, missing=%d (will fetch up to %d this cycle)',
                len(all_envs), len(missing), max_per_cycle)

    # 3. Fetch missing in BATCHES (single PS invocation per batch — review #8)
    # Cap at max_per_cycle so a single cycle doesn't run too long.
    to_fetch = missing[:max_per_cycle]
    FETCH_BATCH = 25  # 25 envelopes per PS invocation
    consecutive_failures = 0   # circuit breaker — review #7
    posted_ok = 0
    posted_fail = 0
    for i in range(0, len(to_fetch), FETCH_BATCH):
        batch = to_fetch[i:i + FETCH_BATCH]
        code, fetched = _run_ps(
            ['-Op', 'fetch', '-EnvelopeIds', ','.join(batch)],
            timeout=_FETCH_TIMEOUT, op_label=f'fetch[{i}]'
        )
        if code != 0:
            consecutive_failures += 1
            if consecutive_failures >= 5:
                logger.error('svh_reconcile: 5 consecutive fetch failures — abort cycle')
                break
            time.sleep(2)
            continue
        consecutive_failures = 0

        for row in fetched:
            if not row.get('ok'):
                logger.debug('svh_reconcile: skip envelope=%s error=%s',
                             row.get('envelope_id'), row.get('error'))
                continue
            env = row['envelope_id']
            msg_type = row.get('msg_type', '')
            try:
                raw_xml = base64.b64decode(row['raw_xml_b64']).decode('utf-8', errors='replace')
            except Exception as e:
                logger.warning('svh_reconcile: b64 decode failed for %s: %s', env, e)
                continue

            payload = {
                'envelope_id': env,
                'msg_type': msg_type,
                'prepared_at': row.get('prepared_at') or '',
                'raw_xml': raw_xml,
                'parsed_meta': {
                    'envelope_id': env,
                    'msg_type': msg_type,
                    'customs_code': row.get('customs_code', ''),
                    'document_id': row.get('document_id', ''),
                    'ref_document_id': row.get('ref_document_id', ''),
                    'source': 'svh_db_reconcile',
                },
            }
            try:
                status, resp_body = post_inbox_fn(cfg, payload)
            except Exception as e:
                logger.warning('svh_reconcile: POST failed for %s: %s', env, e)
                posted_fail += 1
                time.sleep(throttle)
                continue
            if status == 200:
                logger.debug('svh_reconcile: %s envelope=%s → HTTP 200',
                             msg_type, env)
                posted_ok += 1
            else:
                logger.warning('svh_reconcile: %s envelope=%s → HTTP %s body=%s',
                               msg_type, env, status,
                               (resp_body or b'')[:200])
                posted_fail += 1
            time.sleep(throttle)

    logger.info('svh_reconcile: posted ok=%d fail=%d (remaining=%d)',
                posted_ok, posted_fail, max(0, len(missing) - max_per_cycle))


def svh_reconcile_loop(cfg: dict, post_inbox_fn, http_request_fn) -> None:
    """Daemon-thread loop — runs cycle every poll_interval seconds."""
    poll_interval = int(cfg.get('svh_poll_interval', 600))
    logger.info('svh_reconcile_loop start: poll_interval=%ds window_days=%s '
                'max_per_cycle=%s throttle=%s msg_types=%s',
                poll_interval,
                cfg.get('svh_window_days', 7),
                cfg.get('svh_max_per_cycle', 200),
                cfg.get('svh_throttle_sec', 0.2),
                cfg.get('svh_msg_types') or 'CMN.13010,CMN.13029,CMN.13014,CMN.13021')
    while True:
        try:
            svh_reconcile_one_cycle(cfg, post_inbox_fn, http_request_fn)
        except Exception:
            logger.exception('svh_reconcile cycle crashed')
        time.sleep(poll_interval)
