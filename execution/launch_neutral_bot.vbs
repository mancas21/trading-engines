' launch_neutral_bot.vbs - arranca neutral_bot.py --execute --loop sin consola.
' Lo invoca scripts\execution_watchdog.ps1 (tarea programada cada 2 min).
' El estilo 0 + bWaitOnReturn False deja el proceso desacoplado del vigilante.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\Lenovo\trading-engines\execution"
sh.Run """C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"" -u ""C:\Users\Lenovo\trading-engines\execution\neutral_bot.py"" --execute --loop --interval 20", 0, False
