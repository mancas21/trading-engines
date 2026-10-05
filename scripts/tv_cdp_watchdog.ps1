# tv_cdp_watchdog.ps1 — Self-healing guard for TradingView CDP (:9222).
# Runs on a schedule (Task Scheduler). Behaviour:
#   CDP live                  -> no-op (exit 0)
#   TV open WITHOUT CDP flag  -> restart TV with the flag (tv_ensure.ps1)
#   TV not running            -> no-op (do NOT reopen a chart the user closed)
# This closes the exact failure mode where TV is launched from the Start menu
# (MSIX AppContainer drops user env vars, so the CDP flag is lost).
# Log: tv_cdp_watchdog.log (same dir)

param([int]$Port = 9222)

$ErrorActionPreference = 'SilentlyContinue'
$LogFile  = Join-Path $PSScriptRoot 'tv_cdp_watchdog.log'
$Ensure   = Join-Path $PSScriptRoot 'tv_ensure.ps1'

function Write-Log($msg) {
  Add-Content -Path $LogFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
}

function Test-Cdp {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/version" -UseBasicParsing -TimeoutSec 3
    return ($r.StatusCode -eq 200)
  } catch { return $false }
}

if (Test-Cdp) { exit 0 }                                  # healthy — nothing to do

if (-not (Get-Process -Name 'TradingView' -ErrorAction SilentlyContinue)) {
  exit 0                                                  # TV closed on purpose — leave it
}

Write-Log "CDP down but TradingView is open — invoking tv_ensure.ps1 to reattach CDP."
& powershell -ExecutionPolicy Bypass -File $Ensure -Port $Port | Out-Null
if (Test-Cdp) { Write-Log "Recovered: CDP live on :$Port." } else { Write-Log "WARN: recovery failed." }
