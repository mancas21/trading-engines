' launch_reader.vbs — Lanza reader_supervisor.bat completamente DESACOPLADO.
'
' WScript sale inmediatamente; el cmd.exe lanzado no queda ligado a ninguna
' consola llamante, asi que nada de lo que hagamos en una shell lo baja. Es el
' mismo patron que launch_bridge.vbs y launch_qae.vbs.
'
' Recordatorio importante: esto NO controla ATAS ni TradingView. Son aplicaciones
' de escritorio; este supervisor solo lee lo que publican cuando estan abiertas.
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Lenovo\trading-engines\reader"
WshShell.Run "cmd /c C:\Users\Lenovo\trading-engines\reader\reader_supervisor.bat", 0, False
Set WshShell = Nothing