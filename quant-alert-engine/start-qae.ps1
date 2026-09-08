# start-qae.ps1 — Restart the Quant Alert Engine via a detached supervisor.
# Kills any stale supervisor cmd + qae python, then launches a WScript-
# detached supervisor that auto-restarts quant_alert_engine.py on crash.
# Safe to run repeatedly (idempotent).

$ErrorActionPreference = 'SilentlyContinue'

# Kill legacy supervisor cmd.exe
$supers = Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
  Where-Object { $_.CommandLine -like '*qae_supervisor*' }
foreach ($s in @($supers)) {
  Stop-Process -Id $s.ProcessId -Force
  Write-Host "Stopped supervisor cmd $($s.ProcessId)"
}
# Kill any qae python instances (headless engine OR PyQt UI)
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*quant_alert*.py*' }
foreach ($p in @($procs)) {
  Stop-Process -Id $p.ProcessId -Force
  Write-Host "Stopped QAE python PID $($p.ProcessId)"
}

Start-Sleep -Seconds 1

# Launch detached supervisor (survives our shell exit, auto-restarts on crash)
wscript "C:\Users\Lenovo\trading-engines\quant-alert-engine\launch_qae.vbs"
Write-Host "QAE supervisor launched (detached, auto-restart)." -ForegroundColor Green
