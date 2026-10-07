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

# 2026-10-07 — este bucle era INFINITO y SIN TOPE, identico al patron que produjo
# pila de dialogos "Already running" del incidente anterior. Un bot que crashea
# en bucle se levanta miles de veces, y cada intento manda el aviso de arranque a
# Telegram: eso es parte del spam que recibias.
#
# Ahora: espera CRECIENTE entre reinicios y un TECHO de fallos rapidos. Agotado el
# tope, el supervisor sale y deja el bot PARADO y visible — un bucle que nunca
# para es un fallo que hay que mirar, no recuperacion.
$attempt = 0
$fastFails = 0
$lastExit = Get-Date
$MAX_FAST_RESTARTS = 8
$FAST_WINDOW_SEC = 300

while ($true) {
  $attempt++
  Write-Log "Starting bot (attempt #$attempt, fast=$fastFails/$MAX_FAST_RESTARTS)..."
  $proc = Start-Process -FilePath "node" -ArgumentList "telegram-bot.mjs" -NoNewWindow -PassThru -RedirectStandardOutput "bot_stdout.log" -RedirectStandardError "bot_stderr.log"
  Write-Log "Bot PID: $($proc.Id) — running"
  $proc.WaitForExit()
  $code = $proc.ExitCode

  # exit 0 = parada deliberada. Parar es parar: no se relanza.
  if ($code -eq 0) {
    Write-Log "Bot exited cleanly (code 0) — supervisor stopping, nothing relaunched."
    exit 0
  }

  $ranFor = ((Get-Date) - $lastExit).TotalSeconds
  $lastExit = Get-Date
  if ($ranFor -lt $FAST_WINDOW_SEC) { $fastFails++ } else { $fastFails = 0 }
  Write-Log "Bot exited with code $code after $([int]$ranFor)s — fast=$fastFails"

  if ($fastFails -ge $MAX_FAST_RESTARTS) {
    Write-Log "GIVING UP after $fastFails fast restarts. This is NOT recovery, it is a failure. Bot left DOWN."
    exit 1
  }

  $delay = [Math]::Min($RestartDelaySec * [Math]::Pow(2, [Math]::Min($fastFails, 4)), 60)
  Write-Log "Restarting in $delay s..."
  Start-Sleep -Seconds $delay
}
