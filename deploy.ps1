# deploy.ps1 — обновить и перезапустить CargoTrack Pro на VPS
#
# Запуск (PowerShell, в корне C:\cargotrack):
#     .\deploy.ps1
#
# Делает по порядку:
#   1) git pull origin main
#   2) uv sync (поставит/обновит зависимости)
#   3) migrate
#   4) убьёт старый waitress
#   5) запустит свежий waitress в фоне
#   6) проверит health-endpoint
#
# Если упадёт на каком-то шаге — остановится и покажет где именно.

$ErrorActionPreference = 'Stop'

function Step($n, $title) {
    Write-Host ""
    Write-Host "═══ Шаг $n: $title ═══" -ForegroundColor Cyan
}

$projectRoot = "C:\cargotrack"
Set-Location $projectRoot

Step 1 "git pull"
& git pull origin main
$lastCommit = (& git log -1 --oneline) -join ''
Write-Host "  HEAD: $lastCommit" -ForegroundColor Green

Step 2 "uv sync (зависимости)"
& uv sync
Write-Host "  OK" -ForegroundColor Green

Step 3 "migrate"
& uv run python manage.py migrate cargo --noinput
Write-Host "  OK" -ForegroundColor Green

Step 4 "остановить старый waitress"
$old = Get-Process waitress-serve -ErrorAction SilentlyContinue
if ($old) {
    $old | Stop-Process -Force
    Write-Host "  Остановлено процессов: $($old.Count)" -ForegroundColor Green
} else {
    Write-Host "  (waitress-serve.exe не найден — пропускаю)" -ForegroundColor Yellow
}
$oldPy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like '*waitress-serve*cargotrack.wsgi*'
}
if ($oldPy) {
    $oldPy | ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
    }
    Write-Host "  Завершено python.exe-обёрток: $($oldPy.Count)" -ForegroundColor Green
}
Start-Sleep -Seconds 2

Step 5 "запустить свежий waitress"
Start-Process `
    -FilePath "$projectRoot\.venv\Scripts\waitress-serve.exe" `
    -ArgumentList "--listen=127.0.0.1:8000", "--url-scheme=https", "cargotrack.wsgi:application" `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden
Start-Sleep -Seconds 4
$proc = Get-Process waitress-serve -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host "  Запущен, PID: $($proc.Id -join ',')  StartTime: $($proc.StartTime)" -ForegroundColor Green
} else {
    Write-Host "  ! waitress-serve не запустился. Проверь вручную." -ForegroundColor Red
    exit 1
}

Step 6 "проверка health-endpoint"
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/health/" -UseBasicParsing -TimeoutSec 5
    Write-Host "  HTTP $($r.StatusCode) — сервис отвечает." -ForegroundColor Green
} catch {
    Write-Host "  ! Health-check не прошёл: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Готово. CargoTrack Pro обновлён до $lastCommit и работает." -ForegroundColor Green
Write-Host "Теперь в браузере: Ctrl+F5 на странице, и нажми 'Накладная в Альту'." -ForegroundColor Yellow
