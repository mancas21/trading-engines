@echo off
setlocal EnableDelayedExpansion
title QUANT ALERT ENGINE Supervisor
cd /d C:\Users\Lenovo\trading-engines\quant-alert-engine
echo [%date% %time%] QAE supervisor started >> C:\Users\Lenovo\trading-engines\quant-alert-engine\qae_supervisor.log
:loop
echo [%date% %time%] Starting Quant Alert UI (Telegram enabled)... >> C:\Users\Lenovo\trading-engines\quant-alert-engine\qae_supervisor.log
"C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe" C:\Users\Lenovo\trading-engines\quant-alert-engine\quant_alert_ui.py >> C:\Users\Lenovo\trading-engines\quant-alert-engine\qae_stdout.log 2>> C:\Users\Lenovo\trading-engines\quant-alert-engine\qae_stderr.log
echo [%date% %time%] QAE exited (code !ERRORLEVEL!), restarting in 3s... >> C:\Users\Lenovo\trading-engines\quant-alert-engine\qae_supervisor.log
timeout /t 3 /nobreak >nul
goto loop
