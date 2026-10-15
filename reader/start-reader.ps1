# start-reader.ps1 — Arranca el grabador + notificador como servicio desacoplado.
#
# A diferencia del motor, esto NO necesita ATAS ni TradingView abiertos para
# arrancar: el supervisor levanta, y si no hay dato simplemente no graba y lo
# dice. Para tener evidencia hace falta ATAS abierto (es una app de escritorio).
#
# Idempotente: mata cualquier instance previa del supervisor y relanza UNA.

$ErrorActionPreference = 'SilentlyContinue'
$root = 'C:\Users\Lenovo\trading-engines\reader'

# Solo procesos PYTHON del supervisor. El filtro ancho '*reader_supervisor*'
# tambien casa con el cmd.exe que ejecuta reader_supervisor.bat, y contar ese
# era justo lo que permitia declarar "arrancado" sin que hubiera nada.
function Get-ReaderSupervisorPids {
  @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*reader_supervisor.py*' } |
    ForEach-Object { [int]$_.ProcessId })
}

# 1) Primero los procesos del supervisor, o los hijos que tiene ya colgando.
$supers = @(Get-CimInstance Win32_Process |
            Where-Object { $_.CommandLine -like '*reader_supervisor*' })
foreach ($p in $supers) {
  if ($p.ProcessId -eq $PID) { continue }
  Stop-Process -Id $p.ProcessId -Force
  Write-Host "  parado supervisor (PID $($p.ProcessId))" -ForegroundColor Yellow
}
if ($supers.Count -eq 0) { Write-Host "  no habia supervisor corriendo" -ForegroundColor DarkGray }

# 2) Luego los hijos (grabador / notificador) que puedan quedar vivos.
$kids = @(Get-CimInstance Win32_Process |
          Where-Object { $_.CommandLine -like '*verdict_recorder*' -or
                         $_.CommandLine -like '*verdict_notify*' })
foreach ($p in $kids) {
  if ($p.ProcessId -eq $PID) { continue }
  Stop-Process -Id $p.ProcessId -Force
  Write-Host "  parado hijo (PID $($p.ProcessId))" -ForegroundColor Yellow
}

Start-Sleep -Milliseconds 600

# 3) Esperar a que los viejos mueran DE VERDAD.
#    Stop-Process -Force es asincrono. Si el nuevo arranca antes de que el viejo
#    suelte el lock, InstanceLock le hace salir con ALREADY_RUNNING_EXIT (3) y el
#    servicio se queda sin nadie mientras el script cantaba "arrancado".
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
  if ((Get-ReaderSupervisorPids).Count -eq 0) { break }
  Start-Sleep -Milliseconds 300
}
$before = Get-ReaderSupervisorPids
if ($before.Count -gt 0) {
  Write-Host "  AVISO: supervisor(es) viejo(s) siguen vivos: $($before -join ', ')" -ForegroundColor Yellow
}

# 4) Lanzar via wscript SIN -Wait (medido: con -Wait puede colgarse minutos).
#    System.Diagnostics.Process en vez de Start-Process: Start-Process falla en
#    este host.
$launcher = Join-Path $root 'launch_reader.vbs'
if (-not (Test-Path $launcher)) {
  Write-Host "FALTA launcher: $launcher" -ForegroundColor Red
  exit 1
}
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName        = 'wscript.exe'
$psi.Arguments       = "`"$launcher`""
$psi.UseShellExecute = $false
$psi.CreateNoWindow  = $true
[void][System.Diagnostics.Process]::Start($psi)

# 5) Confirmar que arranca DE VERDAD, y con un PID NUEVO.
#    Antes: `@(... '*reader_supervisor*').Count -ge 1` a los 900ms. Si el viejo
#    aun no habia muerto, contaba ESE. Un control que no puede fallar no es un
#    control.
$newPid = $null
$deadline = (Get-Date).AddSeconds(25)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 400
  $cand = @(Get-ReaderSupervisorPids | Where-Object { $before -notcontains $_ })
  if ($cand.Count -ge 1) { $newPid = $cand[0]; break }
}
if (-not $newPid) {
  Write-Host "No arranco ningun supervisor nuevo. Revisa reader\supervisor_stderr.log" -ForegroundColor Red
  exit 1
}
Start-Sleep -Milliseconds 2500
if (-not (Get-Process -Id $newPid -ErrorAction SilentlyContinue)) {
  Write-Host "El supervisor (PID $newPid) arranco y SALIO enseguida — probablemente el" -ForegroundColor Red
  Write-Host "lock seguia tomado (ALREADY_RUNNING_EXIT). Revisa supervisor_launch.log" -ForegroundColor Red
  exit 1
}
Write-Host "Servicio del reader arrancado (PID $newPid, vivo tras 2.5s)" -ForegroundColor Green