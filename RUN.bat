@echo off
REM ============================================================
REM  Start the app. Assumes INSTALL.bat has already been run.
REM  SN Softech Solutions  -  9993199719
REM ============================================================

setlocal EnableExtensions
cd /d "%~dp0"
title AI Bill-to-Tally Agent
color 0B

set "VPY=.venv\Scripts\python.exe"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [X] Not installed yet. Run INSTALL.bat first
    echo      ^(or just double-click START.bat^).
    echo.
    pause
    exit /b 1
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
echo  Starting...  http://localhost:8501
echo  Sign in: demo@snsoftech.local  /  Demo@1234
echo  Keep this window open while using the app.
echo.

".venv\Scripts\python.exe" -m streamlit run app.py --server.port 8501

echo.
echo  The app has stopped.
pause
endlocal
