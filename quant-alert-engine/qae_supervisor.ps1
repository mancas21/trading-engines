# qae_supervisor.ps1 — Supervisor for Quant Alert Engine with health checks.
# Monitors:
#   - quant_alert_engine health (/health)
#   - auto-restarts engine if critical
# Usage: powershell -ExecutionPolicy Bypass -File qae_supervisor.ps1
# Env: QAE_PORT (default 8081), CHECK_INTERVAL_S (default 30)

param(
  [int]$CheckIntervalS = 30,
  [int]$QAEPort = $env:QAE_PORT,
  [string]$LogFile = "qae_supervisor.log"
)

if (-not $QAEPort) { $QAEPort = 8081 }
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$QAE_DIR = "C:\Users\Lenovo\trading-engines\quant-alert-engine"
$PYTHON = "C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"
$ENGINE = "quant_alert_engine.py"

function Write-Log($msg) {
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
  Write-Output $line
  Add-Content -Path (Join-Path $scriptDir $LogFile) -Value $line
}

function Start-Engine {
  Write-Log "Starting Quant Alert Engine..."
  return Start-Process -FilePath $PYTHON -ArgumentList $ENGINE `
    -WorkingDirectory $QAE_DIR -NoNewWindow -PassThru `
    -RedirectStandardOutput "qae_stdout.log" -RedirectStandardError "qae_stderr.log"
}

function Restart-Engine($proc) {
  if ($proc -and -not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force
    Write-Log "Killed engine PID $($proc.Id)"
  }
  Start-Sleep -Seconds 3
  return Start-Engine
}

function Check-Health {
  try {
    $url = "http://127.0.0.1:$QAEPort/health"
    $resp = Invoke-RestMethod -Uri $url -TimeoutSec 5 -ErrorAction Stop
    return $true
  } catch {
    return $false
  }
}

Write-Log "QAE supervisor started — port: $QAEPort interval: ${CheckIntervalS}s"

$proc = Start-Engine
$fails = 0

while ($true) {
  try {
    Start-Sleep -Seconds $CheckIntervalS
    if (-not (Check-Health)) {
      $fails++
      Write-Log "Health check FAILED (streak $fails) — restarting engine..."
      $proc = Restart-Engine $proc
      $fails = 0
    } else {
      $fails = 0
      # Log process health
      if ($proc -and -not $proc.HasExited) {
        Write-Log "Engine alive — PID $($proc.Id)"
      } else {
        Write-Log "Engine exited unexpectedly — restarting..."
        $proc = Start-Engine
      }
    }
  } catch {
    $fails++
    Write-Log "Supervisor error: $($_.Exception.Message) (streak $fails)"
    if ($fails -ge 5) {
      Write-Log "5 consecutive errors — full restart..."
      $proc = Restart-Engine $proc
      $fails = 0
    }
  }
}
