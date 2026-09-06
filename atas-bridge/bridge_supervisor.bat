@echo off
setlocal EnableDelayedExpansion
title ATAS Bridge Supervisor
cd /d C:\atas-bridge
echo [%date% %time%] Bridge supervisor started >> C:\atas-bridge\bridge_supervisor.log
:loop
echo [%date% %time%] Starting bridge... >> C:\atas-bridge\bridge_supervisor.log
"%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe" C:\atas-bridge\bridge_server.py >> C:\atas-bridge\bridge_stdout.log 2>> C:\atas-bridge\bridge_stderr.log
echo [%date% %time%] Bridge exited (code !ERRORLEVEL!), restarting in 3s... >> C:\atas-bridge\bridge_supervisor.log
timeout /t 3 /nobreak >nul
goto loop
