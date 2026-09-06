@echo off
REM ============================================================================
REM QUANT ALERT ENGINE v2.5 - Run Script for Windows 11
REM ============================================================================

setlocal enabledelayedexpansion

echo.
echo ============================================================================
echo QUANT ALERT ENGINE v2.5
echo Real-time Technical Analysis & Alert System
echo ============================================================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please run setup_windows.bat first.
    pause
    exit /b 1
)

REM Activate virtual environment
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
    echo [OK] Virtual environment activated
) else (
    echo [WARNING] Virtual environment not found. Running without venv...
)

echo.
echo Starting Quant Alert Engine v2.5...
echo Configuration: config.json + .env (secrets)
echo Logs (JSON): logs\quant_alert_engine.log
echo Health: http://localhost:8081/health  (if enabled in config)
echo.
echo Press CTRL+C to stop the engine
echo.
echo ============================================================================
echo.

REM Validate config before launch
python -c "from config_schema import Config; c=Config('config.json'); print('[OK] Config valid')" 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Config validation failed - check config.json and .env
    pause
    exit /b 1
)

REM Run the main application
python quant_alert_engine.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Application exited with error code %errorlevel%
    pause
)
