# -*- coding: utf-8 -*-
"""Запускает crm_sync исключая Коробкова Екатерина.

Используется через Start-Process чтобы избежать проблем с кириллицей
в PowerShell ArgumentList.
"""
import os
import subprocess
import sys


def main() -> int:
    os.chdir(r'C:\cargotrack')
    result = subprocess.run(
        [
            r'C:\cargotrack\.venv\Scripts\python.exe',
            '-u', 'manage.py', 'crm_sync',
            '--exclude', 'Коробкова Екатерина',
        ],
        check=False,
    )
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
