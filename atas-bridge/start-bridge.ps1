# start-bridge.ps1 — Restart the ATAS bridge via a detached supervisor.
# Kills any stale supervisor cmd + bridge python, then launches a WScript-
# detached supervisor console that auto-restarts bridge_server.py on crash.
# Safe to run repeatedly (idempotent).

$ErrorActionPreference = 'SilentlyContinue'

# 2026-10-15 — NO MATARSE A SI MISMO.
# El filtro "*bridge_supervisor*" casa con CUALQUIER powershell que lo mencione,
# incluida la shell que invoca este script (basta con que su linea de comandos
# cite el nombre, por ejemplo para diagnosticarlo). MEDIDO: ejecutandolo desde
# una shell de diagnostico, el script mato a esa misma shell — y con ella la
# ejecucion entera — antes de lanzar nada, y el puente se quedo abajo. Se
# excluyen el propio proceso y toda su cadena de ancestros, igual que hace
# bridge_watchdog.ps1 con su guardia de solapamiento.
$ancestors = @{}
$p = $PID
for ($i = 0; $i -lt 10 -and $p -gt 0; $i++) {
  try {
    $p = (Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction Stop).ParentProcessId
    if ($p -gt 0) { $ancestors[[int]$p] = $true }
  } catch { break }
}

function Test-IsSelf([int]$id) {
  return ($id -eq $PID -or $ancestors.ContainsKey($id))
}

# Kill legacy supervisor cmd.exe
$supers = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { ($_.CommandLine -like '*bridge_supervisor*' -or $_.CommandLine -like '*bridge_supervisor.ps1*') -and -not (Test-IsSelf $_.ProcessId) }
foreach ($s in @($supers)) {
  Stop-Process -Id $s.ProcessId -Force
  Write-Host "Stopped supervisor powershell $($s.ProcessId)"
}
$supers = Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
  Where-Object { $_.CommandLine -like '*bridge_supervisor*' -and -not (Test-IsSelf $_.ProcessId) }
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
wscript "C:\Users\Lenovo\trading-engines\atas-bridge\launch_bridge.vbs"
Write-Host "Bridge supervisor launched (detached, auto-restart)." -ForegroundColor Green
