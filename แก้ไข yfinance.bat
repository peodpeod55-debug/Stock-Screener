@echo off
chcp 65001 >nul
title Fix yfinance
echo.
echo  ====================================
echo   Fix yfinance (rate limit / cache)
echo  ====================================
echo.

cd /d "%~dp0"

echo  [1/3] Clearing yfinance global cache...
if exist "%LOCALAPPDATA%\py-yfinance" (
    rmdir /s /q "%LOCALAPPDATA%\py-yfinance"
    echo        OK - cache cleared
) else (
    echo        Skipped - no cache found
)
echo.

echo  [2/3] Clearing local cache folder...
if exist ".yf_cache" (
    rmdir /s /q ".yf_cache"
    echo        OK - local cache cleared
) else (
    echo        Skipped - no local cache found
)
echo.

echo  [3/3] Reinstalling pinned versions (requirements.txt)...
python -m pip install --force-reinstall "yfinance==1.4.1" "curl_cffi==0.15.0"
echo.

echo  ====================================
echo   Done! Now run "Start Bot.bat"
echo  ====================================
echo.
pause
