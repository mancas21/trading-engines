# start-monitor.ps1 — Restart the orderflow monitor via a detached supervisor.
# Kills any stale supervisor cmd + monitor python, then launches a WScript-
# detached supervisor console that auto-restarts orderflow_calls.py monitor
# on crash. Safe to run repeatedly (idempotent).
# CAUTION: only touches processes whose command line matches the monitor —
# never the bridge (bridge_server.py) or QAE.

$ErrorActionPreference = 'SilentlyContinue'

# 2026-10-15 — ESTE SCRIPT NO PODIA FALLAR, Y POR ESO MENTIA.
#
# Hacia: matar todo proceso del monitor, lanzar launch_monitor.vbs (que ejecuta
# `cmd /c ...\monitor_supervisor.bat`) y despedirse con "Monitor supervisor
# launched (detached, auto-restart)" en VERDE. Pero monitor_supervisor.bat NO
# EXISTE en el repo, y el programa que supervisaba tampoco: de orderflow_calls.py
# solo queda un .pyc en __pycache__. O sea que el script apagaba lo que hubiera,
# no arrancaba nada y ademas afirmaba lo contrario. El log del monitor lo
# confirma: su ultima linea es del 2026-09-17.
#
# Ahora se COMPRUEBA ANTES DE TOCAR NADA, para no tumbar un monitor que si
# estuviera funcionando por culpa de un lanzador roto.
$monitorPy  = Join-Path $PSScriptRoot 'orderflow_calls.py'
$monitorBat = Join-Path $PSScriptRoot 'monitor_supervisor.bat'
$launcher   = Join-Path $PSScriptRoot 'launch_monitor.vbs'
$missing = @()
if (-not (Test-Path $monitorPy))  { $missing += 'orderflow_calls.py (el programa)' }
if (-not (Test-Path $monitorBat)) { $missing += 'monitor_supervisor.bat (el supervisor)' }
if (-not (Test-Path $launcher))   { $missing += 'launch_monitor.vbs (el lanzador)' }
if ($missing.Count -gt 0) {
  Write-Host "NO se arranca nada y NO se ha matado nada: falta $($missing -join ', ')." -ForegroundColor Red
  Write-Host "El subsistema /calls (order-flow calls) fue retirado del repositorio." -ForegroundColor Yellow
  Write-Host "Solo quedaron sus lanzadores, apuntando a ficheros que ya no existen." -ForegroundColor Yellow
  Write-Host "Si lo quieres de vuelta, restaura antes el programa y su supervisor." -ForegroundColor Yellow
  exit 1
}

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
