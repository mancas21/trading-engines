@echo off
REM scanner-engine launcher
REM Uses the quant-alert-engine venv: it already has the deps this engine needs
REM (pandas/numpy are unused here, but a second venv is not worth the drift).
setlocal
set ROOT=%~dp0..
set PY=%ROOT%\quant-alert-engine\venv\Scripts\python.exe

if not exist "%PY%" (
  echo venv not found at %PY%
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