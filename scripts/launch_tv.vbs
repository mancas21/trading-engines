' launch_tv.vbs — Detached TradingView-with-CDP launcher (logon path).
' Mirrors launch_qae.vbs / launch_bridge.vbs: WScript exits immediately,
' tv_ensure.ps1 runs hidden and idempotent (no-op if CDP already live).
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Lenovo\trading-engines\scripts"
WshShell.Run "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\Users\Lenovo\trading-engines\scripts\tv_ensure.ps1""", 0, False
Set WshShell = Nothing
