param([int]$Port = 9222)

# Dynamic path resolution (survives Store version updates) — replaces hardcoded version path
. "$PSScriptRoot\Resolve-TvPath.ps1"
$tvPath = Get-TradingViewPath
if (-not $tvPath) {
    Write-Host "Error: TradingView.exe not found (Store, standard locations, and PATH checked)" -ForegroundColor Red
    exit 1
}
Write-Host "Found TradingView at: $tvPath" -ForegroundColor Green
# 2026-10-07: apuntaba a la copia vieja C:\Users\Lenovo\claudeverstradingview.
# Esta es la copia DUPLICADA de tradingview-bridge/scripts/start-tv-bot.ps1 (los dos
# ficheros existen y son distintos). Se corrige la ruta aqui tambien; si solo se
# arreglara uno, el otro volveria a arrancar el codigo viejo.
$projectDir = $PSScriptRoot
if (-not (Test-Path (Join-Path $projectDir 'telegram-bot.mjs'))) {
    $projectDir = Split-Path $PSScriptRoot -Parent
}
if (-not (Test-Path (Join-Path $projectDir 'telegram-bot.mjs'))) {
    Write-Host "Error: telegram-bot.mjs no encontrado cerca de $PSScriptRoot" -ForegroundColor Red
    exit 1
}
Write-Host "Project dir: $projectDir" -ForegroundColor Green

Write-Host "=== Killing existing TV instances ===" -ForegroundColor Yellow
Get-Process -Name "TradingView" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3

Write-Host "=== Launching TV with CDP + no-proxy ===" -ForegroundColor Yellow
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $tvPath
$psi.Arguments = "--no-proxy-server --remote-debugging-port=$Port"
$psi.UseShellExecute = $true
$tvProc = [System.Diagnostics.Process]::Start($psi)
Write-Host "TV PID: $($tvProc.Id)"

Write-Host "=== Waiting for CDP (up to 120s) ===" -ForegroundColor Yellow
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect("127.0.0.1", $Port)
        $tcp.Close()
        Write-Host "CDP ready at http://localhost:$Port" -ForegroundColor Green
        $ready = $true
        break
    } catch {
        $tcp.Dispose()
    }
    Write-Host "." -NoNewline
    Start-Sleep -Seconds 2
}

if (-not $ready) {
    Write-Host "Timeout waiting for CDP" -ForegroundColor Red
    exit 1
}

Start-Sleep -Seconds 5

Write-Host "=== Killing old bot processes ===" -ForegroundColor Yellow
# Kill orphaned node processes that are bots (not MCP server)
$mcpPid = (Get-Process -Id $PID -ErrorAction SilentlyContinue).Id
Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" | Where-Object {
    $_.CommandLine -like "*telegram_bot*"
} | ForEach-Object {
    Write-Host "Killing bot PID $($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force
}

Write-Host "=== Starting Telegram bot ===" -ForegroundColor Yellow
$botPsi = New-Object System.Diagnostics.ProcessStartInfo
$botPsi.FileName = "cmd.exe"
$botPsi.Arguments = '/c "node telegram_bot.mjs > bot.log 2>&1"'
$botPsi.WorkingDirectory = $projectDir
$botPsi.UseShellExecute = $true
$botPsi.CreateNoWindow = $true
$botProc = [System.Diagnostics.Process]::Start($botPsi)
Write-Host "Bot PID: $($botProc.Id)"

Write-Host "=== Done ===" -ForegroundColor Green
Write-Host "TV PID: $($tvProc.Id) | Bot PID: $($botProc.Id)"
