@echo off
REM scanner-engine supervisor launcher.
REM
REM ASCII ONLY. An earlier version had an em-dash inside a REM line and cmd
REM choked on it ("'M' is not recognized as an internal or external command"),
REM because the console codepage read the multi-byte character as a token.
REM Keep every byte of this file in the ASCII range.
REM
REM The child command follows a literal "--" so the supervisor can tell its own
REM flags from the command it should supervise.

setlocal
set HERE=%~dp0
REM 2026-10-11 - DECOUPLED from quant-alert-engine. Was %HERE%..\quant-alert-engine\venv.
REM That venv belongs to QAE; depending on it tied this engine's lifetime to QAE's.
REM Scanner needs stdlib + certifi (optional) only. System Python is enough.
set PY=C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe
set SUP=%HERE%supervisor.py

cd /d "%HERE%"
"%PY%" "%SUP%" --max-attempts 999 --backoff 5 --stall 900 -- "%PY%" scanner.py --interval 12
endlocal