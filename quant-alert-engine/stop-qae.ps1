# stop-qae.ps1 — Stop the Quant Alert Engine Pro completely.
#
# There was no way to stop it before: qae_supervisor.bat relaunched the UI on
# every exit (including a clean close) and it is launched detached via
# launch_qae.vbs, so there was no console window to close either.
#
# Order matters: the SUPERVISOR dies first, otherwise it just resurrects the UI
# three seconds later.
#
#   .\stop-qae.ps1                    stop the UI + its supervisor
#   .\stop-qae.ps1 -IncludeHeadless   also stop the headless engine/supervisor
#   .\stop-qae.ps1 -KeepLock          leave data\engine.lock in place
#
# NOTE: start-qae.ps1 is a RESTART (it kills, then relaunches a detached
# supervisor). Use this script when you actually want it stopped.

param(
    [switch]$IncludeHeadless,
    [switch]$KeepLock
)

$ErrorActionPreference = 'SilentlyContinue'
$root = 'C:\Users\Lenovo\trading-engines\quant-alert-engine'
$stopped = @()

Write-Host "Stopping Quant Alert Engine..." -ForegroundColor Cyan

# 1) Supervisors first, or they restart everything we just killed.
$supers = @(Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
            Where-Object { $_.CommandLine -like '*qae_supervisor*' })
$supers += @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
             Where-Object { $_.CommandLine -like '*qae_supervisor*' })
foreach ($p in $supers) {
    Stop-Process -Id $p.ProcessId -Force
    $stopped += "supervisor $($p.Name) PID $($p.ProcessId)"
    Write-Host "  stopped supervisor ($($p.Name) PID $($p.ProcessId))" -ForegroundColor Yellow
}
if ($supers.Count -eq 0) { Write-Host "  no supervisor running" -ForegroundColor DarkGray }

# 2) Then the UI (and the headless engine only if asked).
$children = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $c = $_.CommandLine
    ($c -like '*quant_alert_ui.py*') -or ($IncludeHeadless -and ($c -like '*quant_alert_engine.py*'))
})
foreach ($p in $children) {
    Stop-Process -Id $p.ProcessId -Force
    $stopped += "python PID $($p.ProcessId)"
    Write-Host "  stopped $($p.CommandLine -replace '.*\\','') (PID $($p.ProcessId))" -ForegroundColor Yellow
}
if ($children.Count -eq 0) { Write-Host "  no UI/engine process running" -ForegroundColor DarkGray }

# 3) Clear now-stale single-instance locks so the next launch is not refused.
# data\engine.lock guards the UI; data\supervisor.lock guards the supervisor.
# Without clearing the latter, a force-killed supervisor leaves a lock whose PID
# may be recycled by an unrelated process, and the next start is refused forever.
foreach ($lockName in @('engine.lock', 'supervisor.lock')) {
    $lock = Join-Path $root "data\$lockName"
    if (-not $KeepLock -and (Test-Path $lock)) {
        Remove-Item $lock -Force
        Write-Host "  cleared stale data\$lockName" -ForegroundColor DarkGray
    }
}

Start-Sleep -Milliseconds 400
# Wait for the OS to actually reap the killed processes before declaring the
# result. A force-kill can still be listed for a moment, which produced a false
# "processes still present" warning right after a successful stop.
$left = @()
for ($i = 0; $i -lt 8; $i++) {
    $left = @(Get-CimInstance Win32_Process |
              Where-Object { $_.CommandLine -like '*quant-alert-engine*' -and
                             ($_.CommandLine -like '*quant_alert_ui.py*' -or
                              $_.CommandLine -like '*qae_supervisor*' -or
                              ($IncludeHeadless -and $_.CommandLine -like '*quant_alert_engine.py*')) })
    if ($left.Count -eq 0) { break }
    Start-Sleep -Milliseconds 500
}

if ($left.Count -eq 0) {
    Write-Host "Quant Alert Engine stopped. Nothing will restart it." -ForegroundColor Green
    exit 0
}
Write-Host "Warning: $($left.Count) process(es) still present:" -ForegroundColor Red
$left | ForEach-Object { Write-Host "  PID $($_.ProcessId) $($_.Name)" }
exit 1
