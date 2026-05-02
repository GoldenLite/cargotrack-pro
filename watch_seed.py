"""
Автоматический пересев тестовых данных каждые 2 минуты.
Запуск из папки проекта (там, где manage.py):

    python watch_seed.py

Ctrl+C — остановить.
"""
import os
import subprocess
import sys
import time
from datetime import datetime

INTERVAL = 120  # секунд

# Папка, где лежит этот скрипт (там же должен быть manage.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Python из виртуального окружения проекта
VENV_PYTHON = os.path.join(BASE_DIR, '..', 'venv', 'Scripts', 'python.exe')
PYTHON = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable


def run_seed():
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    result = subprocess.run(
        [PYTHON, 'manage.py', 'seed', '--clear'],
        capture_output=True,
        cwd=BASE_DIR,
        env=env,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    if result.stdout and result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr and result.stderr.strip():
        stderr_text = result.stderr.strip()
        label = '[ОШИБКА]' if result.returncode != 0 else '[ЛОГ]'
        print(label, stderr_text)
    return result.returncode == 0


def main():
    iteration = 0
    print('Автопересев запущен. Интервал: 2 минуты. Ctrl+C для остановки.\n')

    while True:
        iteration += 1
        ts = datetime.now().strftime('%H:%M:%S')
        print(f'─── Итерация #{iteration}  {ts} ───────────────────────────')

        ok = run_seed()
        status = 'OK' if ok else 'FAIL'
        next_ts = datetime.now().strftime('%H:%M:%S')
        print(f'[{status}] Следующий пересев через {INTERVAL} с. (примерно в {_next_time()})\n')

        try:
            time.sleep(INTERVAL)
        except KeyboardInterrupt:
            print('\nОстановлено.')
            sys.exit(0)


def _next_time():
    import datetime as dt
    return (dt.datetime.now() + dt.timedelta(seconds=INTERVAL)).strftime('%H:%M:%S')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nОстановлено.')
