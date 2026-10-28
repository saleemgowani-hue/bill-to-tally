@echo off
REM ============================================================
REM  Start the app so OTHER PCs on the shop LAN can use it.
REM  Run this on the main PC (usually the one with Tally).
REM  SN Softech Solutions  -  9993199719
REM ============================================================

setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title AI Bill-to-Tally Agent - Network Mode
color 0E

set "VPY=.venv\Scripts\python.exe"

if not exist ".venv\Scripts\python.exe" (
    echo  [X] Not installed yet. Run INSTALL.bat first.
    pause
    exit /b 1
)

REM Find this PC's LAN address to show the staff.
REM PowerShell first because it works on non-English Windows;
REM ipconfig is the fallback.
set "LANIP="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 ^| Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } ^| Select-Object -First 1).IPAddress" 2^>nul`) do set "LANIP=%%i"

if not defined LANIP (
    for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
        if not defined LANIP (
            set "LANIP=%%a"
            set "LANIP=!LANIP: =!"
        )
    )
)

if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
    > "%USERPROFILE%\.streamlit\credentials.toml" echo [general]
    >> "%USERPROFILE%\.streamlit\credentials.toml" echo email = ""
)

REM A package can go missing after an update. Repair rather than crash.
"%VPY%" check_deps.py >nul 2>&1
if errorlevel 1 (
    echo  Updating dependencies, please wait...
    "%VPY%" -m pip install -r requirements.txt
    "%VPY%" check_deps.py --stamp
)

echo.
echo  ============================================================
echo    NETWORK MODE
echo.
echo    On THIS PC      : http://localhost:8501
if defined LANIP echo    On other PCs    : http://!LANIP!:8501
echo.
echo    If other PCs cannot connect, allow Python through
echo    Windows Defender Firewall on the Private network.
echo.
echo    Do NOT expose this to the internet without a VPN.
echo  ============================================================
echo.

".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501 --server.address 0.0.0.0

echo.
pause
endlocal
