@echo off
chcp 65001 >nul
title Install auto-start
set "SU=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%SU%\StockLookupBot.lnk"

echo.
echo  This will make the bot start automatically when Windows boots,
echo  with no console window (start_bot_hidden.vbs -^> run_bot.bat).
echo.

REM old installer wrote a .bat that opened a console window - replace it
if exist "%SU%\StockLookupBot.bat" del "%SU%\StockLookupBot.bat"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%');" ^
  "$s.TargetPath = \"$env:WINDIR\system32\wscript.exe\";" ^
  "$s.Arguments = '\"%~dp0start_bot_hidden.vbs\"';" ^
  "$s.WorkingDirectory = '%~dp0';" ^
  "$s.Save()"

if exist "%LNK%" (
    echo  OK - installed: %LNK%
    echo  To remove auto-start later, delete that shortcut.
) else (
    echo  FAILED - could not create the shortcut in the Startup folder.
)
echo.
pause
