# execution_watchdog.ps1 - 2-minute watchdog for the testnet execution bridge.
#
# WHY THIS EXISTS (2026-10-29)
# ============================
# Measured today: the auto_router and a trigger_entry watcher launched with
# Start-Process died silently between turns (empty stderr, no traceback). The
# watcher was killed before the 15:00 UTC JUP candle close and the breakout
# entry was missed. Exchange-side stops survived (SUI closed at its stop), but
# nothing that depended on a live local process did.
# The repo already solved this class of failure for the scanner and reader:
# a Windows Scheduled Task every 2 minutes + a detached launcher. This does the
# same for the execution bridge.
#
# WHAT IT DOES
# ============
# - neutral_bot: if no python process matches *neutral_bot.py*, launch it
#   detached via launch_neutral_bot.vbs (--loop --execute --interval 20).
#   (antes: auto_router con estrategias S02-S10, retirado el 2026-11-12)
# - (removed 2026-11-02) The ad-hoc JUP watcher. The lone trigger_entry.py was
#   a manual, unvalidated per-symbol entry path. JUP now belongs to the scanner
#   universe and reaches execution only through the engine candidates that
#   auto_router consumes. The state file stays as a cancellation record.
# - Silent when healthy: only launches and failures are logged.
#
# BLAST DOORS
# ===========
# - scripts\.execution_watchdog_pause -> watchdog does nothing (intentional
#   downtime). Delete it to re-arm.
# - It never kills anything. Every start goes through the VBS launchers.
# - WMI is bounded (OperationTimeoutSec 8). If WMI does not answer, this
#   watchdog assumes the process is ALIVE: never launch a duplicate router.
# - ASCII ONLY: no BOM; PS 5.1 can misread non-ASCII bytes.
#
# EXIT CODES: 0 = healthy or relaunched, 1 = still down after relaunch.

param(
  [switch]$Force
)

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent $ScriptDir
$LogFile   = Join-Path $ScriptDir 'execution_watchdog.log'
$PauseFile = Join-Path $ScriptDir '.execution_watchdog_pause'
$ExecDir   = Join-Path $Root 'execution'

if (Test-Path $PauseFile) { exit 0 }

function Write-Log($msg) {
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')][$PID] $msg"
  for ($i = 0; $i -lt 5; $i++) {
    try { Add-Content -Path $LogFile -Value $line -ErrorAction Stop; return }
    catch { Start-Sleep -Milliseconds 200 }
  }
}

# True if a python process whose command line matches $pattern is alive.
# Bounded WMI; conservative on failure (assume alive: duplicates are worse).
function Test-Python([string]$pattern) {
  try {
    $procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -OperationTimeoutSec 8 -ErrorAction Stop
  } catch {
    Write-Log "WMI did not answer; assuming a live process to avoid a duplicate"
    return $true
  }
  foreach ($p in $procs) {
    if ("$($p.CommandLine)" -like $pattern) { return $true }
  }
  return $false
}

$ok = $true

# ---------------- neutral_bot (antes auto_router) ----------------
# 2026-11-12: el auto_router (estrategias S02-S10, direccional) fue retirado.
# Lo sustituye el bot NEUTRAL de grid (neutral_bot.py), que opera los 9 pares
# habilitados sin sesgo direccional. Mismo contrato: si no hay proceso vivo,
# se relanza via el launcher VBS.
if (Test-Python '*neutral_bot.py*') {
  # healthy
} else {
  Write-Log 'neutral: no live neutral_bot.py - launching'
  & wscript.exe (Join-Path $ExecDir 'launch_neutral_bot.vbs')
  Start-Sleep -Seconds 8
  if (Test-Python '*neutral_bot.py*') {
    Write-Log 'neutral: UP'
  } else {
    $ok = $false
    Write-Log 'neutral: STILL DOWN after launch - needs human attention'
  }
}

# ---------------- JUP watcher: REMOVED 2026-11-02 ----------------
# Was: relaunch trigger_entry.py for JUP when no terminal state existed.
# Removed because it was a loose, manual, unvalidated entry path outside the
# scanner/reader pipeline. JUP is now a scanner pair; execution goes through
# auto_router and its engine candidates only.

if ($ok) { exit 0 }
exit 1
