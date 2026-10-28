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
    echo  [X] Python not found. Install Python 3.14 from
    echo      https://www.python.org/downloads/windows/ and tick
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

echo  Setting up offline OCR (Tesseract)...
set "TESS_PATH="
for /f "delims=" %%T in ('where tesseract 2^>nul') do if not defined TESS_PATH set "TESS_PATH=%%T"
if not defined TESS_PATH if exist "%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe" set "TESS_PATH=%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"
if not defined TESS_PATH if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" set "TESS_PATH=%ProgramFiles%\Tesseract-OCR\tesseract.exe"

if defined TESS_PATH (
    echo  [OK] Tesseract already installed.
) else (
    echo        Not found - downloading the official Windows installer
    echo        ^(one-time, about 80 MB^)...
    set "TESS_URL=https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/tesseract-ocr-w64-setup-5.5.3.20260724.exe"
    set "TESS_TEMP=%TEMP%\tesseract-setup.exe"
    set "TESS_DIR=%LOCALAPPDATA%\Tesseract-OCR"
    if exist "%TESS_TEMP%" del /f /q "%TESS_TEMP%" >nul 2>&1
    where curl >nul 2>&1 && curl -L --fail --ssl-no-revoke -o "%TESS_TEMP%" "%TESS_URL%"
    if not exist "%TESS_TEMP%" powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri '%TESS_URL%' -OutFile '%TESS_TEMP%' -UseBasicParsing } catch { Write-Host $_.Exception.Message; exit 1 }"
    if exist "%TESS_TEMP%" (
        echo        Installing to %TESS_DIR% ^(no admin rights needed^)...
        "%TESS_TEMP%" /S /D=%TESS_DIR%
        timeout /t 8 /nobreak >nul
        if exist "%TESS_DIR%\tesseract.exe" (
            set "TESS_PATH=%TESS_DIR%\tesseract.exe"
            echo  [OK] Tesseract installed to %TESS_DIR%
        )
    )
    if not defined TESS_PATH (
        echo  [!] Could not install Tesseract automatically ^(usually a
        echo      firewall/antivirus block, not a problem with this app^).
        echo      Install it by hand instead - one minute:
        echo        1. Open https://github.com/UB-Mannheim/tesseract/wiki
        echo        2. Download the 64-bit installer and run it ^(defaults are fine^)
        echo        3. Paste the install path into Settings -^> AI / OCR in the app
    )
)

if defined TESS_PATH (
    "%VPY%" -c "import pathlib,re;p=pathlib.Path('.env');t=p.read_text(encoding='utf-8');t=re.sub(r'(?m)^TESSERACT_CMD=\s*$', lambda m: 'TESSERACT_CMD=' + r'%TESS_PATH%', t);p.write_text(t,encoding='utf-8')"
)
echo.

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
echo    Double-click RUN.bat every time you want to start the app.
echo.
echo    Offline OCR is ready - RapidOCR (fast, default) and Tesseract
echo    (fallback) are both set up. No API key needed.
echo    Want PaddleOCR / PP-StructureV3 as well? Run
echo    INSTALL_OFFLINE_OCR.bat (optional, one-time).
echo  ============================================================
echo.
pause
endlocal
