@echo off
chcp 65001 >nul
title Install auto-start
set "SU=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

echo.
echo  This will make the bot start automatically when Windows boots.
echo.

(
  echo @echo off
  echo start "" /min "%~dp0เริ่ม Bot.bat"
) > "%SU%\StockLookupBot.bat"

if exist "%SU%\StockLookupBot.bat" (
    echo  OK - installed to Startup folder:
    echo  %SU%\StockLookupBot.bat
    echo.
    echo  To remove auto-start later, delete that file.
) else (
    echo  FAILED - could not write to Startup folder.
)
echo.
pause
