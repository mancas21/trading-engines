@echo off
rem run_neutral_bot.cmd - arranca el bot neutral con stdout/stderr a fichero.
rem Lo invoca launch_neutral_bot.vbs (tarea programada Execution Watchdog).
cd /d "%~dp0"
"C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe" -u neutral_bot.py --execute --loop --interval 20 1>> logs\neutral_bot.log 2>> logs\neutral_bot.err.log
