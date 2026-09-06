@echo off
chcp 65001 >nul
powershell -ExecutionPolicy Bypass -File "%~dp0start-tv-bot.ps1"
pause
