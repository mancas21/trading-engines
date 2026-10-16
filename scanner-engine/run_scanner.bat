@echo off
REM scanner-engine launcher
REM
REM 2026-10-11 - DESACOPLADO de quant-alert-engine. Antes este script usaba
REM   %ROOT%\quant-alert-engine\venv\Scripts\python.exe, lo que ataba este motor
REM   al de QAE: borrar QAE mataba el scanner. Verificado que no hace falta:
REM   las unicas dependencias no-stdlib del scanner son certifi, opcional, con
REM   fallback en http_io.py:40-47 y nada mas. Python del sistema las tiene.
REM   scanner corre con Python312 y su /health responde igual (verificado).
setlocal
set "PY=C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"

if not exist "%PY%" (
  echo python not found at %PY%
  exit /b 1
)

if "%1"=="--once"   goto once
if "%1"=="--audit"  goto audit
if "%1"=="--stop"   goto stop

REM default: run the loop with the HTTP surface on :8090
echo scanner-engine starting on :8090  (Ctrl+C to stop)
"%PY%" "%~dp0scanner.py" %*
exit /b %errorlevel%

:once
"%PY%" "%~dp0scanner.py" --once %*
exit /b %errorlevel%

:audit
"%PY%" "%~dp0expectancy.py" %*
exit /b %errorlevel%

:stop
REM The loop exits cleanly on its own when the console is closed. This script
REM exists for the case where it was launched detached.
echo stopping scanner-engine
taskkill /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq *scanner*" /T 2>nul
exit /b 0