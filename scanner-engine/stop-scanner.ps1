<#
    Stop the scanner-engine supervisor and its scanner child.

    WHY A SCRIPT AND NOT A ONE-LINER
    ===============================
    The supervisor restarts its child on crash (--max-attempts 999), and the
    child is a grandchild of the launcher, so killing the supervisor alone
    leaves the scanner running and vice versa. Measured: killing only the
    supervisor left an orphan scanner writing to the database.

    This kills the whole tree bottom-up, clears the lock, and VERIFIES nothing
    survived -- a stop that reports success while a scanner keeps writing is
    worse than no stop at all.

    Matches on the command line, not the image name, so it never touches the
    OTHER python engines in this repo (quant-alert-engine, atas-bridge) or
    unrelated python on the machine.
#>
param([switch]$Quiet)

$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$lock = Join-Path $here "data\scanner.lock"

function Get-ScannerProcs {
    # 2026-10-15 — BUG ARREGLADO: el filtro era `CommandLine -match
    # 'scanner-engine'`, y el HIJO no lleva esa palabra por ningun lado: se
    # lanza con ruta RELATIVA y cwd=scanner-engine, asi que su CommandLine es
    #     python.exe scanner.py --interval 12
    # Medido en vivo:
    #   PID 29304 supervisor.py -> SI casaba (su CommandLine lleva la ruta
    #                              completa de scanner-engine\supervisor.py)
    #   PID 29296 scanner.py    -> NO casaba (y es EL ESCRITOR de la base)
    # Dos consecuencias, las dos reales:
    #   1. El orden "children first" era un no-op: la lista solo tenia un
    #      proceso, y siempre el supervisor.
    #   2. La verificacion final era CIEGA justo al fallo que este script
    #      existe para prevenir. Su propio docstring lo dice: "killing only the
    #      supervisor left an orphan scanner writing to the database". Si el
    #      hijo queda huerfano, este filtro no lo ve, el script canta
    #      "stopped; lock cleared" y el scanner sigue escribiendo en la base.
    # Se casa ademas por NOMBRE DE SCRIPT, que es lo unico que el hijo lleva
    # encima. El patron es de argumento completo para no barrer nada mas: el
    # unico otro scanner.py del arbol es un fichero de libreria dentro del venv
    # de QAE, que nunca se ejecuta como `python scanner.py`.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -match 'scanner-engine' -or
            $_.CommandLine -match '(^|[\s"])scanner\.py([\s"]|$)'
        }
}

$procs = Get-ScannerProcs
if (-not $procs) {
    if (-not $Quiet) { Write-Host "nothing running" }
    Remove-Item $lock -Force -ErrorAction SilentlyContinue
    exit 0
}

# children first: a supervisor left alive would respawn its child
$ordered = $procs | Sort-Object { if ($_.CommandLine -match 'supervisor\.py') { 1 } else { 0 } }
foreach ($p in $ordered) {
    if (-not $Quiet) {
        $role = if ($p.CommandLine -match 'supervisor\.py') { 'supervisor' } else { 'scanner' }
        Write-Host "  killing $role pid=$($p.ProcessId)"
    }
    # /T takes the whole tree; taskkill is used because Stop-Process will not
    # reach a process started detached under a different session.
    $null = Start-Process -FilePath "taskkill.exe" `
        -ArgumentList "/F","/T","/PID",$p.ProcessId `
        -NoNewWindow -Wait -ErrorAction SilentlyContinue
}

# verify: loop because a respawn can win the race with the kill
for ($i = 1; $i -le 6; $i++) {
    Start-Sleep -Milliseconds 800
    $left = Get-ScannerProcs
    if (-not $left) { break }
    foreach ($p in $left) {
        $null = Start-Process -FilePath "taskkill.exe" `
            -ArgumentList "/F","/T","/PID",$p.ProcessId `
            -NoNewWindow -Wait -ErrorAction SilentlyContinue
    }
}

Remove-Item $lock -Force -ErrorAction SilentlyContinue
$left = Get-ScannerProcs
if ($left) {
    Write-Warning "still running after retries: $($left.ProcessId -join ', ')"
    exit 1
}
if (-not $Quiet) { Write-Host "stopped; lock cleared" }
exit 0