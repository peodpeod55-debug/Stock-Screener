@echo off
REM launcher for start_bot_hidden.vbs (no console) - restart loop like the HK/US bots
chcp 65001 >nul
cd /d "%~dp0"
:loop
python telegram_bot.py
set code=%errorlevel%
if "%code%"=="3" (
    echo another bot instance already holds the lock ^(port 48952^) - this launcher exits
    exit /b 3
)
echo bot exited with code %code%, restarting in 15s...
timeout /t 15 /nobreak >nul
goto loop
