' engines_watchdog.vbs - Runs engines_watchdog.ps1 fully hidden (no console flash).
' Same pattern as atas-bridge\launch_watchdog.vbs and scripts\tv_cdp_watchdog.vbs:
' Task Scheduler runs THIS via wscript.exe (a GUI subsystem binary, no console),
' and the powershell child is spawned with window style 0 = hidden.
' WHY (measured 2026-10-17): the "Scanner+Reader Watchdog" task was created with
' a direct powershell action and flashed a visible console on the desktop every
' 2 minutes; the bridge and TV watchdogs never did, because they launch through
' a VBS like this one.
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Lenovo\trading-engines\scripts"
WshShell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -NonInteractive -File ""C:\Users\Lenovo\trading-engines\scripts\engines_watchdog.ps1""", 0, False
Set WshShell = Nothing
