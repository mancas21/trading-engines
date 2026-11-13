' run_hidden_watchdog.vbs - lanza execution_watchdog.ps1 SIN consola visible.
' La tarea "Execution Watchdog" ejecuta wscript.exe con este archivo: wscript es
' una app GUI (sin consola) y el estilo 0 oculta la ventana de PowerShell, asi
' que no hay parpadeo cada 2 minutos.
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\Lenovo\trading-engines\scripts\execution_watchdog.ps1""", 0, False
