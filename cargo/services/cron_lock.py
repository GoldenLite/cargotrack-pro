"""Файловый лок для cron-команд — с проверкой ЖИВОСТИ владельца.

Почему недостаточно проверки по возрасту (как было до 22.07.2026):
планировщик убивает задачу по `ExecutionTimeLimit`, а reaper — по своему
порогу. Убийство жёсткое: ни `finally`, ни `atexit` не отрабатывают, и
лок-файл остаётся лежать. Пока он «свежий» по возрасту, КАЖДЫЙ следующий
запуск выходит без работы — хотя владельца давно нет.

Кейс 22.07.2026 (жалоба «в CRM протухшие статусы, выпуски не подгружаются»):
`CrmIncSync` с лимитом PT20M убивался на 20-й минуте, а лок считался
протухшим только через 30 → каждые полчаса крон 10 минут работал вхолостую,
а прерванный прогон не доходил до последних вкладок. Внешне всё «зелёное»:
задача завершалась с кодом 0 и сообщением «предыдущий запуск ещё работает».
Диагноз ставился так: PID в `crm_sync_incremental.lock` мёртв, а лок занят.

Поэтому: сначала спрашиваем ОС, жив ли PID из лок-файла. Мёртв — забираем
лок сразу, не дожидаясь возраста. Возраст остаётся страховкой на случай,
когда PID не разобрать (старый формат файла) или он переиспользован ОС.
"""
from __future__ import annotations

import datetime
import logging
import os
import re
import time

logger = logging.getLogger('cargo.cron_lock')

_PID_RE = re.compile(r'pid=(\d+)')


def pid_alive(pid: int) -> bool:
    """Жив ли процесс с таким PID.

    Windows: OpenProcess + GetExitCodeProcess. НЕ `os.kill(pid, 0)` — на
    Windows у os.kill нет семантики «просто проверить», он завершает процесс.
    """
    if not pid or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True          # не смогли узнать — считаем живым (осторожно)
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _owner_pid(path: str) -> int | None:
    try:
        with open(path, 'r') as f:
            m = _PID_RE.search(f.read())
        return int(m.group(1)) if m else None
    except (OSError, ValueError):
        return None


def acquire(path: str, stale_after_sec: int = 30 * 60) -> bool:
    """True — лок наш. False — им реально владеет ЖИВОЙ процесс."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        pid = _owner_pid(path)
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:
            age = 0.0
        if pid is not None and not pid_alive(pid):
            logger.warning(
                'cron_lock: %s — владелец pid=%s МЁРТВ (лок лежит %.1f мин), '
                'перезахватываем', os.path.basename(path), pid, age / 60)
        elif age < stale_after_sec:
            return False
        else:
            logger.warning(
                'cron_lock: %s — лок старше %d мин, перезахватываем',
                os.path.basename(path), stale_after_sec // 60)
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        with open(path, 'w') as f:
            f.write(f'pid={os.getpid()} '
                    f'at={datetime.datetime.now().isoformat()}\n')
        return True
    except OSError:
        return False


def release(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
