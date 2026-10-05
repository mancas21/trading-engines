' tv_cdp_watchdog.vbs — Silent launcher for the CDP watchdog.
' Task Scheduler runs THIS (via wscript.exe), not powershell directly, so no
' console window flashes every interval. Window style 0 = hidden.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -NonInteractive -File ""C:\Users\Lenovo\trading-engines\scripts\tv_cdp_watchdog.ps1""", 0, False
Set WshShell = Nothing
