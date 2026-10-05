# bridge_supervisor.ps1 — Auto-restart wrapper for the ATAS bridge.
# Keeps bridge_server.py alive forever. If the process crashes, restarts after 3s.
# Usage: powershell -ExecutionPolicy Bypass -File bridge_supervisor.ps1
# Log:  bridge_supervisor.log

param(
  [int]$RestartDelaySec = 3,
  [string]$LogFile = "bridge_supervisor.log"
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$python = "C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"
$bridge = "C:\Users\Lenovo\trading-engines\atas-bridge\bridge_server.py"

function Write-Log($msg) {
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
  Write-Output $line
  Add-Content -Path $LogFile -Value $line
}

Write-Log "Bridge supervisor started (dir: $scriptDir)"

$attempt = 0
while ($true) {
  $attempt++
  Write-Log "Starting bridge (attempt #$attempt)..."
  $proc = Start-Process -FilePath $python -ArgumentList $bridge -NoNewWindow -PassThru -RedirectStandardOutput "bridge_stdout.log" -RedirectStandardError "bridge_stderr.log"
  Write-Log "Bridge PID: $($proc.Id) — running"
  $proc.WaitForExit()
  $code = $proc.ExitCode
  Write-Log "Bridge exited with code $code — restarting in $RestartDelaySec s..."
  Start-Sleep -Seconds $RestartDelaySec
}