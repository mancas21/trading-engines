' launch_early_engine.vbs - arranca early_engine.py en modo SHADOW sin consola.
' Convencion del repo (igual que execution/launch_auto_router.vbs): el estilo 0
' deja el proceso desacoplado. 2026-11-15: stdout/stderr REDIRIGIDOS a
' logs/engine_console.log — el motor murio de noche y el crash fue ind
' diagnosticable porque la consola oculta se llevaba la salida.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\Lenovo\trading-engines\early-engine"
sh.Run "cmd /c """"C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"" ""C:\Users\Lenovo\trading-engines\early-engine\early_engine.py"" --config ""C:\Users\Lenovo\trading-engines\early-engine\config.json"" >> ""C:\Users\Lenovo\trading-engines\early-engine\logs\engine_console.log"" 2>&1""", 0, False
