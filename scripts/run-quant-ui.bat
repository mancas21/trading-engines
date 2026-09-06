@echo off
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo.
echo ============================================================================
echo QUANT ALERT ENGINE PRO - Desktop UI
echo Real-time Technical Analysis with Live Dashboard
echo ============================================================================
echo.

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
) else (
    echo [ERROR] Virtual environment not found. Run setup_windows.bat first.
    pause
    exit /b 1
)

echo Starting Quant Alert Engine Pro UI v2.5...
echo Health: http://localhost:8081/health
echo.

REM Validate config before launch
python -c "from config_schema import Config; c=Config('config.json'); print('[OK] Config valid')" 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Config validation failed - check config.json and .env
    pause
    exit /b 1
)

python quant_alert_ui.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] UI exited with code %errorlevel%
    pause
)
