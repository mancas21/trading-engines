@echo off
REM qae_supervisor.bat — thin launcher for qae_supervisor.py.
REM
REM Kept as a .bat (and with this filename) so the existing launch chain keeps
REM working unchanged: launch_qae.vbs ejecuta cmd /c qae_supervisor.bat.
REM (2026-10-15: esta linea tenia una flecha escrita con guion y mayor que, y en
REM un .bat eso NO es texto inerte aunque empiece por REM: cmd procesa la
REM redireccion igual y cada arranque creaba un fichero vacio llamado "cmd".)
REM
REM The restart policy now lives in qae_supervisor.py because batch cannot tell a
REM deliberate close (exit 0) from a crash, which is what made the UI impossible
REM to close.
setlocal
title QUANT ALERT ENGINE Supervisor
cd /d "%~dp0"

REM Same interpreter the supervisor has always used for the UI. Set QAE_PYTHON
REM to switch it (e.g. to venv\Scripts\python.exe).
if not defined QAE_PYTHON set "QAE_PYTHON=C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"

"%QAE_PYTHON%" "%~dp0qae_supervisor.py"
exit /b %ERRORLEVEL%
