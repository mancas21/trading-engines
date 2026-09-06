# start-bridge.ps1 — Restart the ATAS bridge via a detached supervisor.
# Kills any stale supervisor cmd + bridge python, then launches a WScript-
# detached supervisor console that auto-restarts bridge_server.py on crash.
# Safe to run repeatedly (idempotent).

$ErrorActionPreference = 'SilentlyContinue'

# Kill legacy supervisor cmd.exe
$supers = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -like '*bridge_supervisor*' -or $_.CommandLine -like '*bridge_supervisor.ps1*' }
foreach ($s in @($supers)) {
  Stop-Process -Id $s.ProcessId -Force
  Write-Host "Stopped supervisor powershell $($s.ProcessId)"
}
$supers = Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
  Where-Object { $_.CommandLine -like '*bridge_supervisor*' }
foreach ($s in @($supers)) {
  Stop-Process -Id $s.ProcessId -Force
  Write-Host "Stopped supervisor cmd $($s.ProcessId)"
}
# Kill any bridge python instances
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*bridge_server.py*' }
foreach ($p in @($procs)) {
  Stop-Process -Id $p.ProcessId -Force
  Write-Host "Stopped bridge python PID $($p.ProcessId)"
}

Start-Sleep -Seconds 1

# Launch detached supervisor (survives our shell exit, auto-restarts on crash)
wscript "C:\atas-bridge\launch_bridge.vbs"
Write-Host "Bridge supervisor launched (detached, auto-restart)." -ForegroundColor Green
