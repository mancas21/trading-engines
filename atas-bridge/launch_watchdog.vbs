' launch_watchdog.vbs — Runs bridge_watchdog.ps1 fully hidden (no console flash).
' Same pattern as launch_bridge.vbs: WScript exits immediately; the spawned
' powershell has no console window at all, so the 2-minute schedule is silent.
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Lenovo\trading-engines\atas-bridge"
WshShell.Run "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -NonInteractive -NoProfile -File ""C:\Users\Lenovo\trading-engines\atas-bridge\bridge_watchdog.ps1""", 0, False
Set WshShell = Nothing
