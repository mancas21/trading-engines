@echo off
REM reader_supervisor.bat - supervisor del grabador + notificador (reader redesigned).
REM
REM Modelo copiado de bridge_supervisor.bat / qae_supervisor.bat, con la
REM correccion aplicada tras el incidente de los "Already running": el supervisor
REM del motor reiniciaba el hijo en CUALQUIER salida no-cero, producing una pila
REM infinita de dialogos. Aqui la politica esta en reader_supervisor.py, que
REM distingue salida 0 (parada intencionada) de crash real, y se rinde tras 5
REM fallos rapidos seguidos en vez de reiniciar para siempre.
REM
REM ATAS y TradingView son aplicaciones de escritorio de Windows: este supervisor
REM NO las controla ni las reinicia. Si ATAS esta cerrado, el grabador simplemente
REM no graba y lo dice en su log. Para tener datos hace falta ATAS abierto.
setlocal EnableDelayedExpansion
title READER Supervisor (grabador + notificador)
cd /d "%~dp0"

set "PYTHON=C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"

REM Rotacion antes de abrir el log: en este punto la ejecucion anterior termino y
REM su redireccion esta cerrada, que es la unica situacion en la que Windows deja
REM renombrar el fichero. Con el escritor vivo, renombrar y truncar fallan.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\rotate_logs.ps1" >> "%~dp0supervisor_launch.log" 2>&1

echo [%date% %time%] Reader supervisor started >> "%~dp0supervisor_launch.log"
"%PYTHON%" "%~dp0reader_supervisor.py" --loop --interval 30 >> "%~dp0supervisor_stdout.log" 2>> "%~dp0supervisor_stderr.log"
echo [%date% %time%] Reader supervisor exited (code !ERRORLEVEL!) >> "%~dp0supervisor_launch.log"
exit /b !ERRORLEVEL!