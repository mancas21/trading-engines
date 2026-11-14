' launch_early_engine.vbs - arranca early_engine.py en modo SHADOW sin consola.
' Convencion del repo (igual que execution/launch_auto_router.vbs): el estilo 0
' deja el proceso desacoplado; el log de sombra va a early-engine/logs/.
' SOLO UNA INSTANCIA: dos procesos escribirian el mismo JSONL (sombra, sin
' ordenes, pero el estudio se ensuciaria).
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\Lenovo\trading-engines\early-engine"
sh.Run """C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"" ""C:\Users\Lenovo\trading-engines\early-engine\early_engine.py"" --config ""C:\Users\Lenovo\trading-engines\early-engine\config.json""", 0, False
