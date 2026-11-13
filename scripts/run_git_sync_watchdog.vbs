' run_git_sync_watchdog.vbs - lanza git_sync_watchdog.ps1 SIN consola visible.
' La tarea "Git Sync Watchdog" ejecuta wscript.exe con este archivo: wscript es
' una app GUI (sin consola) y el estilo 0 oculta la ventana de PowerShell, asi
' que no hay parpadeo cada 5 minutos.
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\Lenovo\trading-engines\scripts\git_sync_watchdog.ps1""", 0, False
