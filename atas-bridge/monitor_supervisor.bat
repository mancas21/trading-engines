@echo off
setlocal EnableDelayedExpansion
title Orderflow Monitor Supervisor
cd /d C:\Users\Lenovo\trading-engines\atas-bridge
echo [%date% %time%] Monitor supervisor started >> C:\Users\Lenovo\trading-engines\atas-bridge\monitor_supervisor.log
:loop
echo [%date% %time%] Starting monitor... >> C:\Users\Lenovo\trading-engines\atas-bridge\monitor_supervisor.log
"%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe" -u C:\Users\Lenovo\trading-engines\atas-bridge\orderflow_calls.py monitor --log _monitor_payloads.jsonl >> C:\Users\Lenovo\trading-engines\atas-bridge\monitor_stdout.log 2>> C:\Users\Lenovo\trading-engines\atas-bridge\monitor_stderr.log
echo [%date% %time%] Monitor exited (code !ERRORLEVEL!), restarting in 3s... >> C:\Users\Lenovo\trading-engines\atas-bridge\monitor_supervisor.log
timeout /t 3 /nobreak >nul
goto loop
