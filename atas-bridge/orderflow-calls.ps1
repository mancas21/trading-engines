# orderflow-calls.ps1 — run the Binance-order-flow call generator.
#
#   .\orderflow-calls.ps1 start    launch detached (hidden), logging to file
#   .\orderflow-calls.ps1 status   is it running?
#   .\orderflow-calls.ps1 stop     stop it
#
# Deliberately NOT a restart-loop supervisor: a generator that cannot be stopped
# is how the engine ended up resurrecting itself every 3 seconds. If it crashes,
# the log says why and it stays down until you start it again.

param([ValidateSet('start', 'stop', 'status')][string]$Action = 'status')

$root = 'C:\Users\Lenovo\trading-engines\atas-bridge'
$py = 'C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe'
$script = Join-Path $root 'orderflow_calls.py'
$log = Join-Path $root 'orderflow_calls.log'
$errlog = Join-Path $root 'orderflow_calls.log.err'
$payloads = Join-Path $root 'orderflow_calls_payloads.jsonl'
$pidfile = Join-Path $root 'orderflow_calls.pid'

function Get-Running {
    if (-not (Test-Path $pidfile)) { return $null }
    $p = (Get-Content $pidfile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $p) { return $null }
    $proc = Get-Process -Id ([int]$p) -ErrorAction SilentlyContinue
    if ($proc) { return $proc }
    return $null
}

switch ($Action) {
    'status' {
        $proc = Get-Running
        if ($proc) {
            Write-Host "orderflow-calls RUNNING (PID $($proc.Id))" -ForegroundColor Green
            Write-Host "  log: $log"
            if (Test-Path $log) { Get-Content $log -Tail 5 }
        } else {
            Write-Host "orderflow-calls NOT running" -ForegroundColor Yellow
        }
    }
    'start' {
        if (Get-Running) {
            Write-Host "already running (PID $((Get-Running).Id)) — not starting a second one" -ForegroundColor Yellow
            exit 0
        }
        if (-not (Test-Path $script)) { Write-Host "missing $script" -ForegroundColor Red; exit 1 }
        if (-not (Test-Path $py)) { Write-Host "missing interpreter $py" -ForegroundColor Red; exit 1 }
        # -u: unbuffered, so the log is readable while it runs and survives a kill
        $args = @('-u', $script, 'monitor', '--log', $payloads)
        $p = Start-Process -FilePath $py -ArgumentList $args -WorkingDirectory $root `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $log -RedirectStandardError $errlog
        Set-Content -Path $pidfile -Value $p.Id
        Start-Sleep -Seconds 3
        if ($p.HasExited) {
            Write-Host "died immediately (exit $($p.ExitCode)) — last lines:" -ForegroundColor Red
            if (Test-Path $errlog) { Get-Content $errlog -Tail 12 }
            Remove-Item $pidfile -ErrorAction SilentlyContinue
            exit 1
        }
        Write-Host "orderflow-calls started (PID $($p.Id))" -ForegroundColor Green
        Write-Host "  log: $log"
    }
    'stop' {
        $proc = Get-Running
        if (-not $proc) {
            Write-Host "not running" -ForegroundColor DarkGray
            Remove-Item $pidfile -ErrorAction SilentlyContinue
            exit 0
        }
        Stop-Process -Id $proc.Id -Force
        Remove-Item $pidfile -ErrorAction SilentlyContinue
        Write-Host "stopped orderflow-calls (PID $($proc.Id))" -ForegroundColor Green
    }
}
