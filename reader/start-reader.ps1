# start-reader.ps1 — Arranca el grabador + notificador como servicio desacoplado.
#
# A diferencia del motor, esto NO necesita ATAS ni TradingView abiertos para
# arrancar: el supervisor levanta, y si no hay dato simplemente no graba y lo
# dice. Para tener evidencia hace falta ATAS abierto (es una app de escritorio).
#
# Idempotente: mata cualquier instance previa del supervisor y relanza UNA.

$ErrorActionPreference = 'SilentlyContinue'
$root = 'C:\Users\Lenovo\trading-engines\reader'

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

# 3) Lanzar via wscript SIN -Wait (medido: con -Wait puede colgarse minutos).
$launcher = Join-Path $root 'launch_reader.vbs'
if (-not (Test-Path $launcher)) {
  Write-Host "FALTA launcher: $launcher" -ForegroundColor Red
  exit 1
}
Start-Process -FilePath 'wscript.exe' -ArgumentList "`"$launcher`"" -WindowStyle Hidden
Start-Sleep -Milliseconds 900

$now = @(Get-CimInstance Win32_Process |
         Where-Object { $_.CommandLine -like '*reader_supervisor*' })
if ($now.Count -ge 1) {
  Write-Host "Servicio del reader arrancado (supervisores: $($now.Count))" -ForegroundColor Green
} else {
  Write-Host "No se pudo confirmar el arranque. Revisa reader\supervisor_stderr.log" -ForegroundColor Red
  exit 1
}