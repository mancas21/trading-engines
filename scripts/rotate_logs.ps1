# rotate_logs.ps1 - size-based rotation for the logs that NOTHING else rotates.
#
# WHY THIS EXISTS
#   Some logs are not written by a logger: they are opened by a PROCESS
#   REDIRECTION (">>" in a .bat, or Start-Process -RedirectStandardOutput). Those
#   files have no rotation code anywhere, so they only ever grow:
#       qae_stdout.log 10.2 MB, bridge_stdout.log 2.27 MB, bridge_stderr.log 2.05 MB
#   The logs that DO rotate are deliberately absent from the table, so this script
#   and the in-process rotator never fight over the same file:
#       bridge_debug.log, quant_alert_engine.log  -> logging.RotatingFileHandler
#       _tick_history.jsonl                       -> bridge_server.py (20 MB)
#
# THE CONSTRAINT THAT SHAPES THE DESIGN (measured on this machine, 2026-10-16)
#   While the writer holds the handle, Windows refuses to rename, truncate or
#   Clear-Content the file. ONLY READING WORKS:
#       writer alive -> MOVE fails, CLEAR fails, TRUNCATE fails, COPY works
#       writer dead  -> MOVE works
#   So this script NEVER truncates in place. Truncating a file that is being
#   appended to leaves the writer's offset past the new end, which produces a
#   sparse file full of NULs, and it can also cut a half-written line. It RENAMES
#   instead, and when the file is in use it says so and moves on; the file is
#   rotated the next time it is free. Two guaranteed free moments:
#     * the supervisor loop top in bridge_supervisor.bat, where the previous child
#       is gone and cmd has already dropped the ">>" handle (measured: MOVE OK);
#     * this script's own cadence: it is hooked into bridge_watchdog.ps1, a
#       scheduled task ("ATAS Bridge Watchdog") that already runs every 2 minutes.
#   So the control can actually move: idle -> silent, in use -> reported, due ->
#   rotated. Nothing is ever silently skipped.
#
# USAGE
#   powershell -File rotate_logs.ps1            # rotate what is due; silent if idle
#   powershell -File rotate_logs.ps1 -Report    # show the whole table and its state
#   powershell -File rotate_logs.ps1 -DryRun    # show what a real run would do
# Exit: 0 = idle, rotated, or skipped because in use. 1 = a real failure.

param(
  [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
  [switch]$DryRun,
  [switch]$Report
)

$ErrorActionPreference = 'SilentlyContinue'

# name (relative to RepoRoot), CapMB = rotate when BIGGER than this, Keep = how
# many rotated copies to retain. Caps are sized per log: a crash-log that only
# speaks when something breaks (qae_stdout) can afford a bigger cap than a
# per-restart banner (supervisor_launch).
$Logs = @(
  @{ Name = 'atas-bridge\bridge_stdout.log';            CapMB = 5;  Keep = 3 },
  @{ Name = 'atas-bridge\bridge_stderr.log';            CapMB = 5;  Keep = 3 },
  @{ Name = 'atas-bridge\bridge_supervisor.log';        CapMB = 1;  Keep = 2 },
  @{ Name = 'atas-bridge\bridge_watchdog.log';          CapMB = 1;  Keep = 2 },
  @{ Name = 'atas-bridge\monitor_stdout.log';           CapMB = 1;  Keep = 2 },
  @{ Name = 'reader\supervisor_stdout.log';             CapMB = 2;  Keep = 2 },
  @{ Name = 'reader\supervisor_stderr.log';             CapMB = 2;  Keep = 2 },
  @{ Name = 'reader\supervisor_launch.log';             CapMB = 1;  Keep = 2 },
  @{ Name = 'quant-alert-engine\qae_stdout.log';        CapMB = 10; Keep = 2 },
  @{ Name = 'quant-alert-engine\qae_stderr.log';        CapMB = 2;  Keep = 2 },
  @{ Name = 'quant-alert-engine\qae_supervisor.log';    CapMB = 1;  Keep = 2 },
  @{ Name = 'tradingview-bridge\bot.log';               CapMB = 2;  Keep = 2 },
  @{ Name = 'tradingview-bridge\bot_stdout.log';        CapMB = 2;  Keep = 2 },
  @{ Name = 'tradingview-bridge\bot_stderr.log';        CapMB = 2;  Keep = 2 },
  @{ Name = 'tradingview-bridge\bot_supervisor.log';    CapMB = 1;  Keep = 2 },
  @{ Name = 'tradingview-bridge\monitor.log';           CapMB = 1;  Keep = 2 }
)

# Is the file held open by another process? A probe, not a guess: we ask for
# ReadWrite with FileShare::None, which fails on a sharing violation. It is only
# used to CLASSIFY a failed rename (in use vs a real failure), never to decide
# whether to touch the file.
function Test-Locked([string]$Path) {
  try {
    $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $fs.Close()
    return $false
  } catch {
    return $true
  }
}

$failed = $false
$acted = $false

foreach ($spec in $Logs) {
  $rel  = $spec.Name
  $path = Join-Path $RepoRoot $rel
  if (-not (Test-Path -LiteralPath $path)) {
    if ($Report) { Write-Host ("  absent   {0}" -f $rel) }
    continue
  }

  $item  = Get-Item -LiteralPath $path
  $cap   = [double]$spec.CapMB * 1MB
  $mb    = [math]::Round($item.Length / 1MB, 2)

  if ($item.Length -le $cap) {
    if ($Report) { Write-Host ("  ok       {0,-52} {1,8} MB <= {2} MB" -f $rel, $mb, $spec.CapMB) }
    continue
  }

  $dir   = Split-Path -Parent $path
  $leaf  = [IO.Path]::GetFileName($path)
  $base  = [IO.Path]::GetFileNameWithoutExtension($path)
  $ext   = [IO.Path]::GetExtension($path)
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  $dest  = Join-Path $dir ("{0}.{1}{2}" -f $base, $stamp, $ext)

  if ($DryRun) {
    Write-Host ("  WOULD    {0,-52} {1,8} MB -> {2}" -f $rel, $mb, (Split-Path -Leaf $dest))
    continue
  }

  try {
    Move-Item -LiteralPath $path -Destination $dest -ErrorAction Stop
    $acted = $true
    Write-Host ("  ROTATED  {0,-52} {1,8} MB -> {2}" -f $rel, $mb, (Split-Path -Leaf $dest))

    $pattern = Join-Path $dir ("{0}.*{1}" -f $base, $ext)
    $rotated = @(Get-ChildItem -Path $pattern -File -ErrorAction SilentlyContinue |
                 Where-Object { $_.Name -ne $leaf } |
                 Sort-Object Name -Descending)
    $drop = @($rotated | Select-Object -Skip $spec.Keep)
    foreach ($f in $drop) {
      Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
      Write-Host ("  PRUNED   {0}" -f $f.Name)
    }
  } catch {
    if (Test-Locked $path) {
      # The normal, expected case for a log whose process is alive. Not an error:
      # the bridge holding bridge_stdout.log open is not a problem, it is a bridge.
      $acted = $true
      Write-Host ("  IN USE   {0,-52} {1,8} MB - writer alive, will retry when free" -f $rel, $mb)
    } else {
      $failed = $true
      Write-Host ("  FAILED   {0,-52} {1,8} MB - {2}" -f $rel, $mb, $_.Exception.Message)
    }
  }
}

if ($Report) {
  Write-Host ""
  Write-Host ("  repo root: {0}" -f $RepoRoot)
  Write-Host ("  table: {0} logs watched" -f $Logs.Count)
}

if ($failed) { exit 1 }
exit 0
