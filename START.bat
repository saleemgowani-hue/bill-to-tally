@echo off
REM ============================================================
REM  AI Bill-to-Tally Stock Update Agent
REM  SN Softech Solutions  -  Support: 9993199719
REM
REM  Double-click this file. First run installs everything,
REM  later runs just start the app.
REM ============================================================

setlocal EnableExtensions
cd /d "%~dp0"
title AI Bill-to-Tally Agent - SN Softech Solutions
color 0B

echo.
echo  ============================================================
echo    AI BILL-TO-TALLY STOCK UPDATE AGENT
echo    SN Softech Solutions
echo  ============================================================
echo.

REM ---------- 1. Find Python -----------------------------------
set "PYEXE="
py -3 --version >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE (
    python --version >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
    echo  [X] Python was not found on this PC.
    echo.
    echo      Install Python 3.14 from:
    echo          https://www.python.org/downloads/windows/
    echo.
    echo      IMPORTANT: on the first installer screen, tick
    echo          [x] Add Python to PATH
    echo      then run this file again.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('%PYEXE% --version 2^>^&1') do set "PYVER=%%v"
echo  [1/5] Python %PYVER% found.

REM ---------- 2. Virtual environment ---------------------------
if exist ".venv\Scripts\python.exe" (
    echo  [2/5] Using existing environment.
) else (
    echo  [2/5] Creating environment ^(one time, ~30 seconds^)...
    %PYEXE% -m venv .venv
    if errorlevel 1 (
        echo.
        echo  [X] Could not create the environment.
        echo      Try running this file as Administrator.
        pause
        exit /b 1
    )
)

set "VPY=.venv\Scripts\python.exe"

REM ---------- 3. Dependencies ----------------------------------
REM Checked on EVERY launch, not just the first. A one-time marker file used
REM to live here, so a package added by a later version never got installed
REM and the new feature failed with "No module named ...". This repairs
REM itself instead.
"%VPY%" check_deps.py >nul 2>&1
if errorlevel 1 (
    echo  [3/5] Installing dependencies ^(first run takes 2-5 minutes^)...
    echo        Please leave this window open.
    "%VPY%" check_deps.py --list
    "%VPY%" -m pip install --upgrade pip --quiet
    "%VPY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo  [X] Installation failed. The usual cause is no internet
        echo      connection or a company firewall blocking pypi.org.
        echo.
        echo      Contact SN Softech Solutions on 9993199719.
        pause
        exit /b 1
    )
    "%VPY%" check_deps.py --verify >nul 2>&1
    if errorlevel 1 (
        echo.
        echo  [X] Some packages are still missing after installing.
        "%VPY%" check_deps.py --list
        echo.
        echo      Contact SN Softech Solutions on 9993199719.
        pause
        exit /b 1
    )
    "%VPY%" check_deps.py --stamp
    echo        Dependencies ready.
) else (
    echo  [3/5] Dependencies are up to date.
)

REM ---------- 4. Configuration ---------------------------------
if exist ".env" (
    echo  [4/5] Configuration found.
) else (
    echo  [4/5] Creating configuration...
    copy /Y ".env.example" ".env" >nul
    REM Replace the placeholder secret with a real random key
    "%VPY%" -c "import secrets,pathlib;p=pathlib.Path('.env');p.write_text(p.read_text(encoding='utf-8').replace('change-me-to-a-long-random-string',secrets.token_urlsafe(48)),encoding='utf-8')"
    echo        A unique security key was generated for this PC.
)

echo  [4c/5] Setting up offline OCR (Tesseract)...
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

REM ---------- 4a. Licence signing secret --------------------------
REM Generates one on a fresh install. Never touches an existing value:
REM changing it would invalidate every licence key already issued.
"%VPY%" setup_secret.py
if errorlevel 1 (
    echo.
    echo  [X] Could not set up the licence signing secret.
    pause
    exit /b 1
)

REM ---------- 4b. Silence Streamlit's first-run email prompt ----
REM Without this, the very first launch stops and waits for the
REM user to type an email, and the window looks frozen.
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
    > "%USERPROFILE%\.streamlit\credentials.toml" echo [general]
    >> "%USERPROFILE%\.streamlit\credentials.toml" echo email = ""
)

REM ---------- 5. Database --------------------------------------
if exist "data\billtotally.db" (
    echo  [5/5] Database ready.
) else (
    echo  [5/5] Preparing database and demo company...
    "%VPY%" seed_demo.py
    if errorlevel 1 (
        echo.
        echo  [X] Could not prepare the database.
        pause
        exit /b 1
    )
)

echo.
echo  ============================================================
echo    STARTING...  The app opens in your web browser.
echo.
echo    Address : http://localhost:8501
echo    Sign in : demo@snsoftech.local
echo    Password: Demo@1234
echo.
echo    KEEP THIS WINDOW OPEN while you use the app.
echo    Close it ^(or press Ctrl+C^) to stop.
echo  ============================================================
echo.

"%VPY%" -m streamlit run app.py --server.port 8501

echo.
echo  The app has stopped.
pause
endlocal
