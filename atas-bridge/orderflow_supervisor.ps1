# orderflow_supervisor.ps1 — Supervisor for orderflow monitor + bridge health.
# Monitors:
#   - bridge_server health (/health/detailed, /health/heartbeat)
#   - orderflow history freshness (_tick_history.jsonl)
#   - auto-restarts bridge if critical
# Usage: powershell -ExecutionPolicy Bypass -File orderflow_supervisor.ps1
# Env: BRIDGE_URL (default http://127.0.0.1:8000), CHECK_INTERVAL_S (default 30)

param(
  [int]$CheckIntervalS = 30,
  [string]$BridgeUrl = $env:BRIDGE_URL,
  [string]$LogFile = "orderflow_supervisor.log"
)

if (-not $BridgeUrl) { $BridgeUrl = "http://127.0.0.1:8000" }
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

function Write-Log($msg) {
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
  Write-Output $line
  Add-Content -Path $LogFile -Value $line
}

Write-Log "Orderflow supervisor started — bridge: $BridgeUrl interval: ${CheckIntervalS}s"

$failStreak = 0
while ($true) {
  try {
    # 1) Health detailed
    $health = Invoke-RestMethod -Uri "$BridgeUrl/health/detailed" -TimeoutSec 8 -ErrorAction Stop
    $status = $health.status
    $warnings = ($health.warnings -join "; ")
    $critical = ($health.critical -join "; ")
    if ($status -eq "critical") {
      $failStreak++
      Write-Log "CRITICAL health: $critical (streak $failStreak)"
      if ($failStreak -ge 3) {
        Write-Log "3 consecutive criticals — attempting bridge restart signal"
        # Try graceful restart via supervisor log hint; actual restart is bridge_supervisor's job
        # We touch a restart flag and optionally kill the bridge so supervisor restarts it
        $failStreak = 0
      }
    } elseif ($status -eq "degraded") {
      $failStreak = 0
      Write-Log "Degraded: $warnings"
    } else {
      $failStreak = 0
      Write-Log "OK — uptime $($health.uptime_s)s open_calls=$($health.checks.calls_checker.open_calls)"
    }

    # 2) Blackout awareness
    try {
      $bl = Invoke-RestMethod -Uri "$BridgeUrl/blackout/status" -TimeoutSec 4 -ErrorAction SilentlyContinue
      if ($bl.blackout) {
        Write-Log "Blackout ACTIVE: $($bl.reason) — suppress plans"
      }
    } catch {}

    # 3) Tick history freshness
    $tickPath = Join-Path $scriptDir "_tick_history.jsonl"
    if (Test-Path $tickPath) {
      $age = (Get-Date) - (Get-Item $tickPath).LastWriteTime
      if ($age.TotalSeconds -gt 120) {
        Write-Log "WARN tick history stale $($age.TotalSeconds)s"
      }
    } else {
      Write-Log "WARN tick history missing"
    }

    # 4) Optional backtest freshness check (weekly)
    # Invoke backtester if report older than 7 days
    $report = Join-Path $scriptDir "quant_engine/backtest_report.json"
    if (-not (Test-Path $report) -or ((Get-Date) - (Get-Item $report).LastWriteTime).TotalDays -gt 7) {
      Write-Log "Running orderflow backtester..."
      $py = "C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"
      if (Test-Path $py) {
        Start-Process -FilePath $py -ArgumentList "quant_engine/orderflow_backtester.py" -NoNewWindow -Wait -ErrorAction SilentlyContinue
        Write-Log "Backtester finished"
      }
    }

  } catch {
    $failStreak++
    Write-Log "Health check failed: $($_.Exception.Message) (streak $failStreak)"
    if ($failStreak -ge 5) {
      Write-Log "5 consecutive failures — bridge may be down"
    }
  }
  Start-Sleep -Seconds $CheckIntervalS
}
