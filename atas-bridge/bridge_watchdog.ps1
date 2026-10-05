# bridge_watchdog.ps1 — HTTP-aware watchdog for the ATAS bridge.
# The process supervisor (bridge_supervisor.bat) only restarts on PROCESS EXIT,
# but the observed failure mode is process-ALIVE + HTTP port dead. This script
# probes /health/heartbeat and restarts the bridge when it stops answering.
# Safe to run repeatedly (idempotent). Designed for a Windows Scheduled Task
# every 2 minutes. Exit code 0 = healthy or recovered, 1 = still dead.
#
# CAUTION: this script's own filename/command line must NEVER match
# '*bridge_supervisor*' or start-bridge.ps1 will kill it. It doesn't.

$ErrorActionPreference = 'SilentlyContinue'
# Never render progress bars (known to stall Invoke-WebRequest in headless hosts)
$ProgressPreference = 'SilentlyContinue'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
$LogFile = Join-Path $scriptDir "bridge_watchdog.log"
$StateFile = Join-Path $scriptDir ".watchdog_failcount"
$MaxStaleSec = 120
$TimeoutSec = 10

function Write-Log($msg) {
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')][$PID] $msg"
  # Write-Host, NEVER Write-Output: output-stream writes become part of a
  # function's return value, turning every Test-Bridge/Restart-Bridge failure
  # into $true (non-empty array is truthy). That single mistake disabled the
  # entire watchdog: all probes reported healthy forever.
  Write-Host $line
  # Retry on lock contention: two watchdog instances must never lose lines silently
  for ($i = 0; $i -lt 5; $i++) {
    try {
      Add-Content -Path $LogFile -Value $line -ErrorAction Stop
      return
    } catch {
      Start-Sleep -Milliseconds 200
    }
  }
}

function Get-FailCount {
  if (Test-Path $StateFile) {
    $v = Get-Content $StateFile -Raw
    $n = 0
    if ([int]::TryParse($v.Trim(), [ref]$n)) { return $n }
  }
  return 0
}

function Set-FailCount($n) {
  Set-Content -Path $StateFile -Value "$n" -NoNewline
}

function Test-Bridge {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health/heartbeat" -TimeoutSec $TimeoutSec -UseBasicParsing
    if ($r.StatusCode -ne 200) { return $false }
    $j = $r.Content | ConvertFrom-Json
    $age = [double]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) - [double]$j.last_tick
    if ($age -gt $MaxStaleSec) {
      Write-Log "UNHEALTHY: heartbeat stale (last_tick ${age}s ago, max ${MaxStaleSec}s)"
      return $false
    }
    return $true
  } catch {
    Write-Log "UNHEALTHY: probe failed ($($_.Exception.Message))"
    return $false
  }
}

function Restart-Bridge {
  Write-Log "Restarting bridge (killing bridge_server.py processes; supervisor relaunches in ~3s)..."
  $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*bridge_server.py*' }
  foreach ($p in @($procs)) {
    Stop-Process -Id $p.ProcessId -Force
    Write-Log "Killed bridge python PID $($p.ProcessId)"
  }
  Start-Sleep -Seconds 15
  if (Test-Bridge) {
    Write-Log "RECOVERED: bridge answering after restart"
    return $true
  }
  Write-Log "Still dead after kill — full supervisor relaunch via start-bridge.ps1"
  powershell -ExecutionPolicy Bypass -File (Join-Path $scriptDir "start-bridge.ps1") | Out-Null
  Start-Sleep -Seconds 20
  if (Test-Bridge) {
    Write-Log "RECOVERED: bridge answering after full relaunch"
    return $true
  }
  Write-Log "STILL DEAD after full relaunch — needs human attention"
  return $false
}

# --- main ---
$runStart = Get-Date
# Overlap guard: if a previous run is still alive, kill it when stale (>3 min,
# i.e. hung — a healthy run takes seconds) or yield to it when fresh.
# Ancestor processes (our own launcher chain, whose command lines also contain
# this script's path) are NEVER candidates — otherwise every run would see
# itself and exit quietly.
$ancestors = @{}
$p = $PID
for ($i = 0; $i -lt 10 -and $p -gt 0; $i++) {
  try {
    $p = (Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction Stop).ParentProcessId
    if ($p -gt 0) { $ancestors[$p] = $true }
  } catch { break }
}
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.ProcessId -ne $PID -and -not $ancestors.ContainsKey($_.ProcessId) -and $_.CommandLine -match 'bridge_watchdog\.ps1' } |
  ForEach-Object {
    $ageMin = ((Get-Date) - $_.CreationDate).TotalMinutes
    if ($ageMin -gt 3) {
      Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
      Write-Log "Killed stale watchdog PID $($_.ProcessId) (age $([int]$ageMin)m)"
    } else {
      Write-Log "Another watchdog instance active (PID $($_.ProcessId)) — exiting quietly"
      exit 0
    }
  }
Write-Log "Watchdog run started"
if (Test-Bridge) {
  if ((Get-FailCount) -ne 0) { Set-FailCount 0 }
  Write-Log "Watchdog run finished: healthy"
  exit 0
}

$fails = (Get-FailCount) + 1
Set-FailCount $fails
Write-Log "Health check failed (consecutive failures: $fails)"
if ($fails -lt 2) {
  Write-Log "Waiting one more cycle before restart (flap guard)"
  exit 0
}

Set-FailCount 0
if (Restart-Bridge) { Write-Log "Watchdog run finished: recovered"; exit 0 } else { Write-Log "Watchdog run finished: STILL DEAD"; exit 1 }
