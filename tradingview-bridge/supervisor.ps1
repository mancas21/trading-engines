# supervisor.ps1 — Auto-restart wrapper for telegram-bot.mjs
# Keeps the bot alive forever. If the process crashes, restarts after 3s.
# Usage: powershell -ExecutionPolicy Bypass -File supervisor.ps1
# Log:  bot_supervisor.log

param(
  [int]$RestartDelaySec = 3,
  [string]$LogFile = "bot_supervisor.log"
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

function Write-Log($msg) {
  $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
  Write-Output $line
  Add-Content -Path $LogFile -Value $line
}

Write-Log "Supervisor started (dir: $scriptDir)"

$attempt = 0
while ($true) {
  $attempt++
  Write-Log "Starting bot (attempt #$attempt)..."
  $proc = Start-Process -FilePath "node" -ArgumentList "telegram-bot.mjs" -NoNewWindow -PassThru -RedirectStandardOutput "bot_stdout.log" -RedirectStandardError "bot_stderr.log"
  Write-Log "Bot PID: $($proc.Id) — running"
  $proc.WaitForExit()
  $code = $proc.ExitCode
  Write-Log "Bot exited with code $code — restarting in $RestartDelaySec s..."
  Start-Sleep -Seconds $RestartDelaySec
}
