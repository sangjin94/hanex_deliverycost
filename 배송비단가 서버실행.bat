@echo off
title HanEx Delivery Pricing Server
cd /d "%~dp0"

echo ============================================
echo    HanEx Delivery Pricing System - SERVER
echo ============================================
echo.
echo  Browser will open automatically in a moment.
echo  URL: http://localhost:5000
echo.
echo  * To STOP the server, just close this window.
echo ============================================
echo.

start "" /min cmd /c "timeout /t 3 >nul & start http://localhost:5000"

where py >nul 2>nul
if %errorlevel%==0 (
    py app.py
) else (
    python app.py
)

echo.
echo Server stopped.
pause
