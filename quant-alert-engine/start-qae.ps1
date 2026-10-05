# start-qae.ps1 — Restart the Quant Alert Engine via a detached supervisor.
# Kills any stale supervisor cmd + qae python, then launches a WScript-
# detached supervisor that auto-restarts quant_alert_engine.py on crash.
# Safe to run repeatedly (idempotent).

$ErrorActionPreference = 'SilentlyContinue'

# Belt-and-braces: -ErrorAction on the queries themselves too, so a single WMI
# hiccup can't abort the script before it reaches the launch step.
$ErrorActionPreference = 'Continue'

# Kill legacy supervisor cmd.exe
$supers = Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*qae_supervisor*' }
foreach ($s in @($supers)) {
  if ($s.ProcessId -eq $PID) { continue }
  Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue
  Write-Host "Stopped supervisor cmd $($s.ProcessId)"
}
# Kill any qae python instances (headless engine OR PyQt UI)
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*quant_alert*.py*' }
foreach ($p in @($procs)) {
  if ($p.ProcessId -eq $PID) { continue }
  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
  Write-Host "Stopped QAE python PID $($p.ProcessId)"
}

Start-Sleep -Seconds 1

# Launch detached supervisor (survives our shell exit, auto-restarts on crash).
# Launch wscript WITHOUT -Wait. Measured: `Start-Process -Wait` on this wscript
# blocks 0.8s -> 19.2s and in some runs never returns at all, because wscript
# hands the supervisor a console it is still attached to. wscript itself exits
# immediately (Run ..., 0, False in launch_qae.vbs), so there is nothing to
# wait for; waiting only re-attaches us to the child tree's handles.
$launcher = "C:\Users\Lenovo\trading-engines\quant-alert-engine\launch_qae.vbs"
if (-not (Test-Path $launcher)) {
  Write-Host "MISSING launcher: $launcher" -ForegroundColor Red
  exit 1
}
Start-Process -FilePath "wscript.exe" -ArgumentList "`"$launcher`"" -WindowStyle Hidden
Start-Sleep -Milliseconds 700
Write-Host "QAE supervisor launched (detached, auto-restart)." -ForegroundColor Green
