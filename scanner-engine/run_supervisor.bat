@echo off
REM scanner-engine supervisor — the actual command, in a .bat so Windows
REM handles the quoting. Nested quoted exes on one WScript.Shell.Run line are
REM fragile; a batch file has no such problem.
setlocal
set HERE=%~dp0
set PY=%HERE%..\quant-alert-engine\venv\Scripts\python.exe
set SUP=%HERE%supervisor.py

cd /d "%HERE%"
"%PY%" "%SUP%" --max-attempts 999 --backoff 5 --stall 900 -- "%PY%" scanner.py --interval 12
endlocal