' launch_monitor.vbs — Launches monitor_supervisor.bat fully DETACHED.
' WScript exits immediately; the spawned cmd.exe has no console link to any
' caller (including opencode's PowerShell process tree), so nothing our shell
' can do will bring this monitor down.
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Lenovo\trading-engines\atas-bridge"
WshShell.Run "cmd /c C:\Users\Lenovo\trading-engines\atas-bridge\monitor_supervisor.bat", 0, False
Set WshShell = Nothing
