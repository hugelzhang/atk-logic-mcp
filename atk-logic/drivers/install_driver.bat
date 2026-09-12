@echo off
rem ============================================================
rem  ATK-Logic WinUSB driver installer (via Zadig automation)
rem  Plug in the logic analyzer FIRST, then run this AS ADMIN.
rem  Uses the package venv python (with pywinauto) if present.
rem ============================================================
cd /d "%~dp0"
set "PY=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv python not found. Run ..\..\install.bat first.
    pause & exit /b 1
)

echo Requesting admin rights ...
net session >nul 2>&1
if errorlevel 1 (
    powershell -Command "Start-Process -Verb RunAs -FilePath '%~dp0install_driver.bat'"
    exit /b
)

echo Installing WinUSB for ATK-Logic-Analyzer (VID_1A86 PID_FFCC) ...
del /q "%~dp0zadig_auto.log" 2>nul
"%PY%" "%~dp0drive_zadig.py"
echo.
echo Done. Check log: %~dp0zadig_auto.log
pause
