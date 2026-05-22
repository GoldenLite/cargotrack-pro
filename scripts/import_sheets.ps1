#requires -Version 5.1
<#
.SYNOPSIS
  Запуск авто-импорта из Google Sheets — Общее + CRM последовательно.

.DESCRIPTION
  Вызывается из Windows Task Scheduler на VPS. Регистрация задачи (один раз):

    schtasks /Create /SC MINUTE /MO 15 /TN "CargoTrack-SheetsImport" `
        /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\cargotrack\scripts\import_sheets.ps1" `
        /RU SYSTEM /F

  Логи: C:\cargotrack\logs\sheets_import.log (rotation вручную не делаем —
  на 12K строк прогон ~1 МБ при unchanged-fast-path, выживёт долго).
#>

$ErrorActionPreference = 'Stop'

$ROOT     = 'C:\cargotrack'
$VENV_PY  = Join-Path $ROOT '.venv\Scripts\python.exe'
$MANAGE   = Join-Path $ROOT 'manage.py'
$LOG_DIR  = Join-Path $ROOT 'logs'
$LOG_FILE = Join-Path $LOG_DIR 'sheets_import.log'

if (-not (Test-Path $LOG_DIR)) { New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null }

function Write-Log($msg) {
    $line = '{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $msg
    Add-Content -Path $LOG_FILE -Value $line -Encoding UTF8
}

function Run-Import($source) {
    Write-Log "=== START $source ==="
    try {
        $output = & $VENV_PY $MANAGE import_sheets --source $source 2>&1
        $exit = $LASTEXITCODE
        foreach ($l in $output) { Write-Log $l }
        Write-Log "=== END $source (exit=$exit) ==="
    } catch {
        Write-Log "EXCEPTION ${source}: $($_.Exception.Message)"
    }
}

Set-Location $ROOT
Write-Log '============================================================'
Run-Import 'general'
Run-Import 'crm'
