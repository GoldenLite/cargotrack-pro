# -*- coding: utf-8 -*-
"""Запускает crm_sync исключая Коробкова Екатерина.

Пишет лог самостоятельно — schtasks /tr `1>` редирект не работает,
PowerShell ArgumentList ломается на кириллице.
"""
import os
import subprocess
import sys


LOG_PATH = r'C:\cargotrack\crm_sync.log'


def main() -> int:
    os.chdir(r'C:\cargotrack')
    with open(LOG_PATH, 'w', encoding='utf-8') as log:
        log.write(f'Starting crm_sync --exclude "Коробкова Екатерина"\n')
        log.flush()
        result = subprocess.run(
            [
                r'C:\cargotrack\.venv\Scripts\python.exe',
                '-u', 'manage.py', 'crm_sync',
                '--exclude', 'Коробкова Екатерина',
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        log.write(f'\nExited with code {result.returncode}\n')
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
