@echo off
rem ============================================================
rem  01 - ATK-Logic (Logic Analyzer) MCP installer
rem  Steps: create venv + install deps + register MCP "atk-logic"
rem ============================================================
setlocal
cd /d "%~dp0"
set "NAME=atk-logic"
set "SERVER=%~dp0atk-logic\server.py"
set "PY=%~dp0.venv\Scripts\python.exe"

echo [1/4] Check Python ...
python --version >nul 2>&1 || (
    echo   [ERROR] Python not found. Install 64-bit Python 3.10+ first,
    echo   tick "Add Python to PATH" during install.
    echo   https://www.python.org/downloads/
    pause & exit /b 1
)

echo [2/4] Create venv .venv ...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv || ( echo   [ERROR] venv create failed & pause & exit /b 1 )
)

echo [3/4] Install dependencies ...
"%PY%" -m pip install --upgrade pip -q
"%PY%" -m pip install -r requirements.txt || ( echo   [ERROR] pip install failed & pause & exit /b 1 )

echo [4/4] Register MCP "%NAME%" (user scope) ...
claude mcp add "%NAME%" --scope user -- "%PY%" "%SERVER%"
if errorlevel 1 (
    echo   [WARN] auto-register failed. Run manually:
    echo     claude mcp add "%NAME%" --scope user -- "%PY%" "%SERVER%"
)

echo.
echo ============================================================
echo  DONE. MCP name: %NAME%
echo  Verify:  claude mcp list
echo  Driver :  run drivers\install_driver.bat (as admin) FIRST
echo            to install WinUSB for the logic analyzer,
echo            then plug in the device.
echo ============================================================
pause
