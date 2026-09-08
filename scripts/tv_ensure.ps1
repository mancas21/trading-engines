# tv_ensure.ps1 — Idempotent TradingView-with-CDP launcher.
# Logon path (via launch_tv.vbs + Startup shortcut) and on-demand use share this.
# States handled:
#   1. CDP live on :9222            -> nothing to do (exit 0)
#   2. TV open WITHOUT the CDP flag -> restart it WITH the flag (exit 0/1)
#   3. TV not running               -> launch it WITH the flag (exit 0/1)
# NOTE: state 2 closes the user's open charts to reattach CDP. At logon this
# never triggers (nothing open yet). Run on-demand only when that is acceptable.
# Log: tv_autostart.log (same dir)

param([int]$Port = 9222)

$ErrorActionPreference = 'SilentlyContinue'
$LogFile = Join-Path $PSScriptRoot 'tv_autostart.log'

function Write-Log($msg) {
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
  Add-Content -Path $LogFile -Value $line
  Write-Host $line
}

function Test-Cdp {
  try {
    $r = Invoke-WebRequest -Uri "http://localhost:$Port/json/version" -UseBasicParsing -TimeoutSec 3
    return ($r.StatusCode -eq 200)
  } catch { return $false }
}

# State 1: already debuggable — the case a CDP-only probe gets right.
if (Test-Cdp) {
  Write-Log "CDP already live on :$Port — nothing to do."
  exit 0
}

. "$PSScriptRoot\Resolve-TvPath.ps1"
$tvPath = Get-TradingViewPath
if (-not $tvPath) {
  Write-Log "ERROR: TradingView.exe not found (Store, standard locations, PATH checked)."
  exit 1
}

# State 2: open but not debuggable — the case a CDP-only probe MISSES
# (reports 'down' while the user stares at open charts).
if (Get-Process -Name 'TradingView' -ErrorAction SilentlyContinue) {
  Write-Log "TradingView open WITHOUT CDP flag — restarting with --remote-debugging-port=$Port ..."
  Get-Process -Name 'TradingView' | Stop-Process -Force
  for ($i = 0; $i -lt 20 -and (Get-Process -Name 'TradingView' -ErrorAction SilentlyContinue); $i++) {
    Start-Sleep -Seconds 1
  }
} else {
  # State 3: cold start (normal logon path).
  Write-Log "TradingView not running — launching with CDP on :$Port ..."
}

Start-Process -FilePath $tvPath -ArgumentList "--remote-debugging-port=$Port"
for ($i = 0; $i -lt 30; $i++) {
  if (Test-Cdp) { Write-Log "CDP ready on :$Port."; exit 0 }
  Start-Sleep -Seconds 2
}
Write-Log "ERROR: CDP not up after 60s."
exit 1
