' launch_auto_router.vbs - arranca auto_router.py --loop --execute sin consola.
' Lo invoca scripts\execution_watchdog.ps1 (tarea programada cada 2 min).
' El estilo 0 + bWaitOnReturn False deja el proceso desacoplado del vigilante.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\Lenovo\trading-engines\execution"
sh.Run """C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"" ""C:\Users\Lenovo\trading-engines\execution\auto_router.py"" --loop --execute --interval 300", 0, False
