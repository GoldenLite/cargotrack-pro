# -*- coding: utf-8 -*-
"""Запускает crm_reindex с логом в файл."""
import os
import subprocess
import sys


LOG_PATH = r'C:\cargotrack\crm_reindex.log'


def main() -> int:
    os.chdir(r'C:\cargotrack')
    with open(LOG_PATH, 'w', encoding='utf-8') as log:
        log.write('Starting crm_reindex\n')
        log.flush()
        result = subprocess.run(
            [
                r'C:\cargotrack\.venv\Scripts\python.exe',
                '-u', 'manage.py', 'crm_reindex',
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        log.write(f'\nExited with code {result.returncode}\n')
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
