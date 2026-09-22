@echo off
REM ============================================================
REM  Installer only - sets everything up but does not start.
REM  Use this when preparing a customer PC in advance.
REM  SN Softech Solutions  -  9993199719
REM ============================================================

setlocal EnableExtensions
cd /d "%~dp0"
title Install - AI Bill-to-Tally Agent
color 0B

echo.
echo  ============================================================
echo    INSTALLING AI BILL-TO-TALLY STOCK UPDATE AGENT
echo  ============================================================
echo.

set "PYEXE="
py -3 --version >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE (
    python --version >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
    echo  [X] Python not found. Install Python 3.10+ from
    echo      https://www.python.org/downloads/ and tick
    echo      "Add Python to PATH" during setup.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('%PYEXE% --version 2^>^&1') do set "PYVER=%%v"
echo  Python %PYVER%
echo.

echo  Creating environment...
if not exist ".venv\Scripts\python.exe" %PYEXE% -m venv .venv
set "VPY=.venv\Scripts\python.exe"

echo  Installing dependencies...
"%VPY%" -m pip install --upgrade pip --quiet
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo  [X] Dependency installation failed. Check the internet connection.
    pause
    exit /b 1
)
"%VPY%" check_deps.py --verify >nul 2>&1
if errorlevel 1 (
    echo  [X] Some packages are still missing:
    "%VPY%" check_deps.py --list
    pause
    exit /b 1
)
"%VPY%" check_deps.py --stamp

if not exist ".env" (
    echo  Writing configuration...
    copy /Y ".env.example" ".env" >nul
    "%VPY%" -c "import secrets,pathlib;p=pathlib.Path('.env');p.write_text(p.read_text(encoding='utf-8').replace('change-me-to-a-long-random-string',secrets.token_urlsafe(48)),encoding='utf-8')"
)

echo  Setting up the licence signing secret...
".venv\Scripts\python.exe" setup_secret.py

if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
    > "%USERPROFILE%\.streamlit\credentials.toml" echo [general]
    >> "%USERPROFILE%\.streamlit\credentials.toml" echo email = ""
)

if not exist "data\billtotally.db" (
    echo  Preparing database...
    "%VPY%" seed_demo.py
)

echo.
echo  Verifying the installation...
"%VPY%" -m pytest tests -q
if errorlevel 1 (
    echo.
    echo  [!] Some checks did not pass. The app may still run, but
    echo      please report this to SN Softech Solutions - 9993199719.
) else (
    echo.
    echo  All checks passed.
)

echo.
echo  ============================================================
echo    INSTALLATION COMPLETE
echo    Double-click START.bat to run the app.
echo  ============================================================
echo.
pause
endlocal
