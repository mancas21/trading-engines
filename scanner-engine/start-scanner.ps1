<#
    Start the scanner-engine supervisor, detached.

    Uses wscript + launch_scanner.vbs rather than Start-Process so the
    supervisor does not inherit this shell's lifetime. A supervisor started as
    a shell child stopped logging at "attempt 4 launching" and never recorded
    the kill it owed 20 minutes later.

    Do NOT use -Wait on wscript: it keeps the caller attached to the detached
    tree's console handles and hangs. That exact mistake cost 420s on QAE's
    start script.
#>
$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
# 2026-10-11 — DESACOPLADO de quant-alert-engine. Antes: ..\quant-alert-engine\venv.
# Ese venv es de QAE; depender de el ataba este motor al de QAE. El scanner solo
# necesita stdlib + certifi (opcional). Python del sistema sirve.
$py   = "C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"
$vbs  = Join-Path $here "launch_scanner.vbs"
$lock = Join-Path $here "data\scanner.lock"

if (-not (Test-Path $py))   { Write-Error "python not found: $py"; exit 1 }
if (-not (Test-Path $vbs))  { Write-Error "launcher not found: $vbs"; exit 1 }

# Is one already running? The supervisor's own lock is authoritative, but a
# stale lock from a hard-killed process must not block a legitimate start.
$existing = $null
if (Test-Path $lock) {
    $pidInLock = (Get-Content $lock -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($pidInLock) {
        $proc = Get-Process -Id $pidInLock -ErrorAction SilentlyContinue
        if ($proc) {
            $existing = $proc
        } else {
            Write-Host "clearing stale lock (pid $pidInLock is gone)"
            Remove-Item $lock -Force -ErrorAction SilentlyContinue
        }
    }
}

if ($existing) {
    Write-Host "scanner already running (pid $($existing.Id)) - nothing to do"
    exit 0
}

Write-Host "starting scanner-engine supervisor (detached)"
$ws = New-Object -ComObject WScript.Shell
$null = $ws.Run("wscript.exe `"$vbs`"", 0, $false)

# 700ms: enough for wscript to hand off, NOT a readiness poll.
Start-Sleep -Milliseconds 700
Write-Host "launched; health on http://127.0.0.1:8090/health"