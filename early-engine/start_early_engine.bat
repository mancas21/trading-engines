@echo off
REM early-engine launcher (repo convention, same as scanner-engine/run_scanner.bat)
REM
REM MODE: config.json ships "mode": "shadow" by default -- it does NOT post to
REM   the bridge, does NOT send Telegram, does NOT place orders. It only writes
REM   the shadow log logs/early_events.jsonl and samples the unconditional
REM   control every 60 s. Switching to "live" (bridge posting) requires the
REM   study.py excess-over-control to hold across several independent days.
setlocal
set "PY=C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe"

if not exist "%PY%" (
  echo python not found at %PY%
  exit /b 1
)

pushd "%~dp0"
echo early-engine starting in SHADOW mode (logs/early_events.jsonl, Ctrl+C to stop)
"%PY%" "%~dp0early_engine.py" --config "%~dp0config.json" %*
popd
exit /b %errorlevel%
