# deploy.ps1 -- update and restart CargoTrack Pro on the VPS.
#
# Run (PowerShell, from C:\cargotrack):
#     .\deploy.ps1
#
# Steps: git pull -> uv sync -> migrate -> stop old waitress -> start new -> health check.

$ErrorActionPreference = 'Stop'

function Step($n, $title) {
    Write-Host ""
    Write-Host "=== Step $($n): $title ===" -ForegroundColor Cyan
}

$projectRoot = "C:\cargotrack"
Set-Location $projectRoot

Step 1 "git pull"
& git pull origin main
$lastCommit = (& git log -1 --oneline) -join ''
Write-Host "  HEAD: $lastCommit" -ForegroundColor Green

Step 2 "uv sync"
& uv sync
Write-Host "  OK" -ForegroundColor Green

Step 3 "migrate"
& uv run python manage.py migrate cargo --noinput
Write-Host "  OK" -ForegroundColor Green

Step 4 "stop old waitress"
$old = Get-Process waitress-serve -ErrorAction SilentlyContinue
if ($old) {
    $old | Stop-Process -Force
    Write-Host "  stopped waitress-serve processes: $($old.Count)" -ForegroundColor Green
} else {
    Write-Host "  no waitress-serve found, skipping" -ForegroundColor Yellow
}
$oldPy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like '*waitress-serve*cargotrack.wsgi*'
}
if ($oldPy) {
    $oldPy | ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
    }
    Write-Host "  killed python.exe wrappers: $($oldPy.Count)" -ForegroundColor Green
}
Start-Sleep -Seconds 2

Step 5 "start fresh waitress"
Start-Process `
    -FilePath "$projectRoot\.venv\Scripts\waitress-serve.exe" `
    -ArgumentList "--listen=127.0.0.1:8000", "--url-scheme=https", "cargotrack.wsgi:application" `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden
Start-Sleep -Seconds 4
$proc = Get-Process waitress-serve -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host "  started, PID: $($proc.Id -join ',')  StartTime: $($proc.StartTime)" -ForegroundColor Green
} else {
    Write-Host "  ERROR: waitress-serve did not start. Check manually." -ForegroundColor Red
    exit 1
}

Step 6 "health check"
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/health/" -UseBasicParsing -TimeoutSec 5
    Write-Host "  HTTP $($r.StatusCode) -- service is up." -ForegroundColor Green
} catch {
    Write-Host "  ERROR: health check failed: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Done. CargoTrack Pro is at $lastCommit and running." -ForegroundColor Green
Write-Host "Browser: Ctrl+F5 on the HAWB page, then click the green button." -ForegroundColor Yellow
