param([int]$Port = 9222)

# Dynamic path resolution (survives Store version updates) — replaces hardcoded version path
. "$PSScriptRoot\Resolve-TvPath.ps1"
$tvPath = Get-TradingViewPath

if (-not $tvPath) {
    Write-Host "Error: TradingView.exe not found (Store, standard locations, and PATH checked)" -ForegroundColor Red
    exit 1
}
Write-Host "Found TradingView at: $tvPath" -ForegroundColor Green

# Kill existing instances and WAIT until every process is gone. Zombie Electron
# children from a previous launch keep the CDP port/user-data-dir locked, so a
# fresh start binds --remote-debugging-port only after a full process teardown.
Get-Process -Name "TradingView" -ErrorAction SilentlyContinue | Stop-Process -Force
for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-Process -Name "TradingView" -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Seconds 1
}

Write-Host "Starting TradingView with CDP on port $Port..." -ForegroundColor Yellow
Start-Process -FilePath $tvPath -ArgumentList "--remote-debugging-port=$Port"

for ($i = 0; $i -lt 60; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:$Port/json/version" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) {
            Write-Host "`nCDP ready at http://localhost:$Port" -ForegroundColor Green
            exit 0
        }
    } catch {}
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 2
}

Write-Host "`nTimeout: CDP not available after 120 seconds" -ForegroundColor Red
exit 1
