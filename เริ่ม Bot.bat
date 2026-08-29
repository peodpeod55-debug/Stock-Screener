@echo off
chcp 65001 >nul
title Stock Lookup Bot
cd /d "%~dp0"

:loop
echo.
echo  ====================================
echo   Stock Lookup Bot - starting...
echo   (Ctrl+C = stop normally)
echo  ====================================
echo.
python telegram_bot.py
if errorlevel 3 if not errorlevel 4 (
    echo.
    echo  Another bot instance is already running ^(port 48952 held^) - this window exits.
    echo  Stop the other bot first, then start again.
    timeout /t 10 >nul
    exit /b 3
)
if errorlevel 1 (
    echo.
    echo  Bot crashed - restarting in 15 seconds...
    echo  ^(close this window to cancel^)
    timeout /t 15 /nobreak >nul
    goto loop
)
echo.
echo  Bot stopped normally. Press Enter to close.
pause >nul
