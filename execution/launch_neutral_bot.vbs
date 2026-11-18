' launch_neutral_bot.vbs - arranca neutral_bot.py --execute --loop sin consola.
' Lo invoca scripts\execution_watchdog.ps1 (tarea programada cada 2 min).
' Desde 2026-11-17 pasa por run_neutral_bot.cmd para capturar stdout/stderr:
' el proceso moria ~1 vez al dia y no quedaba rastro de la causa.
' El estilo 0 + bWaitOnReturn False deja el proceso desacoplado del vigilante.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\Lenovo\trading-engines\execution"
sh.Run "cmd /c run_neutral_bot.cmd", 0, False
