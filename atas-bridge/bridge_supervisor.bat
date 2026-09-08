@echo off
setlocal EnableDelayedExpansion
title ATAS Bridge Supervisor
cd /d C:\Users\Lenovo\trading-engines\atas-bridge
echo [%date% %time%] Bridge supervisor started >> C:\Users\Lenovo\trading-engines\atas-bridge\bridge_supervisor.log
:loop
echo [%date% %time%] Starting bridge... >> C:\Users\Lenovo\trading-engines\atas-bridge\bridge_supervisor.log
"%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe" C:\Users\Lenovo\trading-engines\atas-bridge\bridge_server.py >> C:\Users\Lenovo\trading-engines\atas-bridge\bridge_stdout.log 2>> C:\Users\Lenovo\trading-engines\atas-bridge\bridge_stderr.log
echo [%date% %time%] Bridge exited (code !ERRORLEVEL!), restarting in 3s... >> C:\Users\Lenovo\trading-engines\atas-bridge\bridge_supervisor.log
timeout /t 3 /nobreak >nul
goto loop
