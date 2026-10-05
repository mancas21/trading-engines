# start-monitor.ps1 — Restart the orderflow monitor via a detached supervisor.
# Kills any stale supervisor cmd + monitor python, then launches a WScript-
# detached supervisor console that auto-restarts orderflow_calls.py monitor
# on crash. Safe to run repeatedly (idempotent).
# CAUTION: only touches processes whose command line matches the monitor —
# never the bridge (bridge_server.py) or QAE.

$ErrorActionPreference = 'SilentlyContinue'

# Kill legacy supervisor cmd.exe for the MONITOR only
$supers = Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
  Where-Object { $_.CommandLine -like '*monitor_supervisor*' }
foreach ($s in @($supers)) {
  Stop-Process -Id $s.ProcessId -Force
  Write-Host "Stopped monitor supervisor cmd $($s.ProcessId)"
}
# Kill any monitor python instances (orderflow_calls only — not bridge/QAE)
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*orderflow_calls*' }
foreach ($p in @($procs)) {
  Stop-Process -Id $p.ProcessId -Force
  Write-Host "Stopped monitor python PID $($p.ProcessId)"
}

Start-Sleep -Seconds 1

# Launch detached supervisor (survives our shell exit, auto-restart on crash)
wscript "C:\Users\Lenovo\trading-engines\atas-bridge\launch_monitor.vbs"
Write-Host "Monitor supervisor launched (detached, auto-restart)." -ForegroundColor Green
