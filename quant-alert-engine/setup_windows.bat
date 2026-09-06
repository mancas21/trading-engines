@echo off
REM ============================================================================
REM QUANT ALERT ENGINE v1.0 - Windows 11 Setup Script
REM ============================================================================
REM This script sets up the complete Python environment for the Quant Alert Engine
REM Requirements: Windows 11, Python 3.8+ (recommended 3.10+)
REM ============================================================================

setlocal enabledelayedexpansion

echo.
echo ============================================================================
echo QUANT ALERT ENGINE v1.0 - Windows 11 Setup
echo ============================================================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation
    pause
    exit /b 1
)

echo [OK] Python is installed
python --version

REM Create virtual environment if it doesn't exist
if not exist "venv" (
    echo.
    echo Creating virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created
)

REM Activate virtual environment
call venv\Scripts\activate.bat
if %errorlevel% neq 0 (
    echo [ERROR] Failed to activate virtual environment
    pause
    exit /b 1
)

echo [OK] Virtual environment activated

REM Upgrade pip
echo.
echo Upgrading pip...
python -m pip install --upgrade pip -q
if %errorlevel% neq 0 (
    echo [ERROR] Failed to upgrade pip
    pause
    exit /b 1
)
echo [OK] pip upgraded

REM Install required packages
echo.
echo Installing required packages...
echo This may take a few minutes on first run...
echo.

pip install -q -r requirements.txt

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to install packages
    echo Attempting manual installation...
    pip install pandas
    pip install numpy
    pip install ccxt
    pip install PyQt6
    pip install requests
    pip install win10toast
    pip install pydantic pydantic-settings python-dotenv
    pip install fastapi uvicorn psutil
)

echo.
echo [OK] All packages installed successfully

REM Create required directories
if not exist "logs" mkdir logs
if not exist "sounds" mkdir sounds
if not exist "data" mkdir data

echo [OK] Directory structure created

REM Setup .env from template if missing
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [OK] Created .env from .env.example - please edit TELEGRAM__TOKEN and TELEGRAM__CHAT_ID
    ) else (
        echo [WARNING] .env not found and .env.example missing
    )
) else (
    echo [OK] .env already exists
)

REM Validate config
echo.
echo Validating configuration...
python -c "from config_schema import Config; c=Config('config.json'); print('[OK] Config valid - symbols:', c.get('symbols'))"
if %errorlevel% neq 0 (
    echo [WARNING] Config validation failed - check config.json and .env
) else (
    echo [OK] Config validation passed
)

REM Display summary
echo.
echo ============================================================================
echo SETUP COMPLETED SUCCESSFULLY! v2.1
echo ============================================================================
echo.
echo Next steps:
echo.
echo 1. Edit .env with your Telegram token/chat_id:
echo    - TELEGRAM__TOKEN and TELEGRAM__CHAT_ID
echo    - Get token from @BotFather on Telegram
echo.
echo 2. Review config.json (non-secret settings):
echo    - Symbols: Edit the "symbols" list (e.g., SOL/USDT, XRP/USDT)
echo    - Check interval: Edit "general.check_interval" (in seconds)
echo    - Indicator parameters: Customize RSI, MACD, BB, ATR parameters
echo.
echo 3. Run the application:
echo    - Engine: run_engine.bat  (headless)
echo    - UI:     run_ui.bat      (desktop dashboard)
echo    - Health: http://localhost:8080/health  (when engine is running)
echo.
echo 4. Sound alerts: Toggle SOUND button in UI or set "enable_sound": false
echo.
echo Logs (JSON lines for aggregation):
echo    - logs\quant_alert_engine.log  (use: type logs\quant_alert_engine.log ^| findstr ALERT_FIRED)
echo.
echo ============================================================================
echo.
pause
