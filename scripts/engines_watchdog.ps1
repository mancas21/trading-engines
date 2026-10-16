# engines_watchdog.ps1 - 2-minute watchdog for scanner-engine and reader.
#
# WHY THIS EXISTS (2026-10-16)
# ===========================
# Measured today: at 12:41:57 local a hard kill (exit code -1, the signature of
# Stop-Process -Force / Task Manager, NOT of any stop script in this repo) took
# down every live python process: the scanner tree, the reader supervisor, the
# ATAS bridge and the EDGE recorders. The bridge and the recorders came back on
# their own (their own watchdogs/supervisors); the SCANNER and the READER did
# NOT, because their only automatic rearm was the login Startup link.
#   - the scanner stayed dead 5h15m: its supervisor died with its child and
#     left a stale lock (pid 17016). Every relaunch attempt from 13:00 to 14:10
#     was refused with "another supervisor already holds the lock" because the
#     stale pid had been RECYCLED by an unrelated live process; after 14:10
#     nobody retried.
#   - the reader stayed dead for the same 5h20m (same kill, same gap: no
#     watchdog, only the login link). Its last ledger row was 12:41:53.
# This closes that gap for BOTH engines, same pattern as the ATAS bridge
# watchdog: a Windows Scheduled Task every 2 minutes.
#
# WHAT IT DOES
# ============
# - scanner: probes http://127.0.0.1:8090/health. After 2 consecutive failed
#   runs (flap guard; -Force skips it) it validates scanner.lock: if the pid is
#   dead, unreadable, or its command line is NOT a scanner process (recycled
#   pid, measured today), the lock is removed. Then start-scanner.ps1 (detached,
#   idempotent) is run and the port is re-probed for up to ~48s.
# - reader: checks that the supervisor process exists AND that
#   reader/supervisor_status.json has a cycle younger than 300s (measured
#   cadence: one cycle every ~30s, and last_cycle is written on every cycle
#   even when ATAS is closed - see reader_supervisor.py:287). Same lock
#   validation (reader_supervisor.lock, expecting a reader_supervisor process)
#   and recovery via start-reader.ps1.
# - Silent when healthy: only failures and actions are logged.
#
# BLAST DOORS
# ===========
# - scripts\.engines_watchdog_pause  -> if this file exists the watchdog does
#   nothing and both engines stay parked. Create it for intentional downtime of
#   the engines; delete it to re-arm.
# - This watchdog NEVER kills processes itself. Every restart goes through
#   start-scanner.ps1 / start-reader.ps1, which (since 2026-10-16) carry
#   ancestor guards and can not kill their caller.
# - Write-Host, never Write-Output, inside functions: in a sibling watchdog an
#   output-stream write made every check truthy forever (bridge_watchdog.ps1
#   header documents that failure). Do not repeat it here.
# - ASCII ONLY: this file has no BOM; PS 5.1 can misread non-ASCII bytes.
#
# EXIT CODES: 0 = healthy or recovered, 1 = still dead after recovery attempt.

param(
  [switch]$Force,          # skip the flap guard (manual/emergency runs)
  [int]$TimeoutSec = 6
)

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root       = Split-Path -Parent $ScriptDir
$LogFile    = Join-Path $ScriptDir 'engines_watchdog.log'
$PauseFile  = Join-Path $ScriptDir '.engines_watchdog_pause'
$ScannerDir = Join-Path $Root 'scanner-engine'
$ReaderDir  = Join-Path $Root 'reader'

if (Test-Path $PauseFile) { exit 0 }

function Write-Log($msg) {
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')][$PID] $msg"
  for ($i = 0; $i -lt 5; $i++) {
    try { Add-Content -Path $LogFile -Value $line -ErrorAction Stop; return }
    catch { Start-Sleep -Milliseconds 200 }
  }
}

function Get-Fails([string]$which) {
  $f = Join-Path $ScriptDir (".engines_watchdog_{0}_fails" -f $which)
  if (Test-Path $f) {
    $v = Get-Content $f -Raw
    $n = 0
    if ([int]::TryParse($v.Trim(), [ref]$n)) { return $n }
  }
  return 0
}

function Set-Fails([string]$which, [int]$n) {
  Set-Content -Path (Join-Path $ScriptDir (".engines_watchdog_{0}_fails" -f $which)) -Value "$n" -NoNewline
}

function Test-Scanner {
  try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8090/health' -TimeoutSec $TimeoutSec -UseBasicParsing
    return ($r.StatusCode -eq 200)
  } catch { return $false }
}

function Test-Reader {
  $sup = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
           Where-Object { $_.CommandLine -like '*reader_supervisor*' })
  if ($sup.Count -eq 0) { return $false }
  $sf = Join-Path $ReaderDir 'supervisor_status.json'
  if (-not (Test-Path $sf)) { return $false }
  try { $j = Get-Content $sf -Raw | ConvertFrom-Json } catch { return $false }
  if (-not $j.last_cycle_iso) { return $false }
  $age = ((Get-Date) - [datetime]$j.last_cycle_iso).TotalSeconds
  return ($age -le 300)
}

# Remove an instance lock ONLY when its holder is provably not the real service:
# dead pid, unreadable pid, or a live pid whose command line does not match any
# of $CmdPatterns (= the pid was recycled by an unrelated process; measured on
# 2026-10-16: this is exactly what blocked the manual relaunches from 13:00 to
# 14:10 while the tree was dead). A legit holder is never touched.
function Clear-StaleLock([string]$LockPath, [string[]]$CmdPatterns) {
  if (-not (Test-Path $LockPath)) { return $false }
  $txt = Get-Content $LockPath -Raw
  $pidTxt = ($txt -replace '[^0-9]', '')
  if (-not $pidTxt) { Remove-Item $LockPath -Force; return $true }
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$pidTxt" -ErrorAction SilentlyContinue
  if ($p) {
    $cl = "$($p.CommandLine)"
    foreach ($pat in $CmdPatterns) { if ($cl -like $pat) { return $false } }
  }
  Remove-Item $LockPath -Force -ErrorAction SilentlyContinue
  return $true
}

function Invoke-StartScript([string]$ScriptPath) {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ScriptPath 2>&1 |
    ForEach-Object { Write-Log ("  start: " + $_) }
}

$recovered = $true

# ---------------- scanner ----------------
if (-not (Test-Scanner)) {
  $fails = (Get-Fails 'scanner') + 1
  Set-Fails 'scanner' $fails
  if ($fails -lt 2 -and -not $Force) {
    Write-Log "scanner unhealthy (consecutive: $fails) - waiting one more cycle (flap guard)"
  } else {
    Write-Log "scanner unhealthy (consecutive: $fails) - recovering"
    $lock = Join-Path $ScannerDir 'data\scanner.lock'
    if (Clear-StaleLock $lock @('*scanner-engine*', '*scanner.py*')) {
      Write-Log "scanner: stale or recycled lock removed"
    }
    Invoke-StartScript (Join-Path $ScannerDir 'start-scanner.ps1')
    $ok = $false
    for ($i = 0; $i -lt 6 -and -not $ok; $i++) {
      Start-Sleep -Seconds 8
      if (Test-Scanner) { $ok = $true }
    }
    if ($ok) { Set-Fails 'scanner' 0; Write-Log 'scanner RECOVERED' }
    else { $recovered = $false; Write-Log 'scanner STILL DEAD after relaunch - needs human attention' }
  }
} else {
  if ((Get-Fails 'scanner') -ne 0) { Set-Fails 'scanner' 0 }
}

# ---------------- reader ----------------
if (-not (Test-Reader)) {
  $fails = (Get-Fails 'reader') + 1
  Set-Fails 'reader' $fails
  if ($fails -lt 2 -and -not $Force) {
    Write-Log "reader unhealthy (consecutive: $fails) - waiting one more cycle (flap guard)"
  } else {
    Write-Log "reader unhealthy (consecutive: $fails) - recovering"
    $lock = Join-Path $ReaderDir 'reader_supervisor.lock'
    if (Clear-StaleLock $lock @('*reader_supervisor*')) {
      Write-Log "reader: stale or recycled lock removed"
    }
    Invoke-StartScript (Join-Path $ReaderDir 'start-reader.ps1')
    $ok = $false
    for ($i = 0; $i -lt 8 -and -not $ok; $i++) {
      Start-Sleep -Seconds 10
      if (Test-Reader) { $ok = $true }
    }
    if ($ok) { Set-Fails 'reader' 0; Write-Log 'reader RECOVERED' }
    else { $recovered = $false; Write-Log 'reader STILL DEAD after relaunch - needs human attention' }
  }
} else {
  if ((Get-Fails 'reader') -ne 0) { Set-Fails 'reader' 0 }
}

if ($recovered) { exit 0 }
exit 1
