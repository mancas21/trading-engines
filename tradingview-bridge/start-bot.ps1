# start-bot.ps1 — One-command startup for the Telegram bot supervisor.
# Run:  powershell -ExecutionPolicy Bypass -File start-bot.ps1
# Spawns supervisor.ps1 hidden (auto-restart) — safe to run repeatedly (idempotent).

$ErrorActionPreference = 'SilentlyContinue'

# Kill any duplicate supervisors so only one bot poller runs (prevents 409).
$supers = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -like '*supervisor.ps1*' }
foreach ($s in @($supers)) {
  if ($s.ProcessId -ne $PID) {
    $children = Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
      Where-Object { $_.ParentProcessId -eq $s.ProcessId }
    $children | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Stop-Process -Id $s.ProcessId -Force
    Write-Host "Stopped duplicate supervisor $($s.ProcessId)"
  }
}
Start-Sleep -Seconds 1

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Start-Process -FilePath "powershell" -ArgumentList "-ExecutionPolicy","Bypass","-File","$scriptDir\supervisor.ps1" -WindowStyle Hidden
Write-Host "Supervisor started (hidden, auto-restart). Bot logs in $scriptDir"
