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
    # ВАЖНО: НЕ редиректим stderr внутри PowerShell (2>&1) — PS 5.1
    # оборачивает каждую строку stderr в ErrorRecord и при ErrorActionPreference=Stop
    # сразу падает. Django logs идут в stderr — отправляем их в файл напрямую
    # через Start-Process, который не делает обёртку.
    $stdoutFile = Join-Path $env:TEMP "sheets_${source}_out.log"
    $stderrFile = Join-Path $env:TEMP "sheets_${source}_err.log"
    $proc = Start-Process -FilePath $VENV_PY `
        -ArgumentList @($MANAGE, 'import_sheets', '--source', $source) `
        -WorkingDirectory $ROOT `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $stdoutFile `
        -RedirectStandardError  $stderrFile
    $exit = $proc.ExitCode
    foreach ($f in @($stdoutFile, $stderrFile)) {
        if (Test-Path $f) {
            # Python пишет UTF-8 в stderr (Django logging) и stdout. Без -Encoding
            # UTF8 PS 5.1 читает как cp1251 → кириллица превращается в «РћР±С‰РµРµ».
            Get-Content $f -Encoding UTF8 | ForEach-Object { Write-Log $_ }
            Remove-Item $f -ErrorAction SilentlyContinue
        }
    }
    Write-Log "=== END $source (exit=$exit) ==="
}

Set-Location $ROOT
Write-Log '============================================================'
Run-Import 'general'
Run-Import 'crm'
