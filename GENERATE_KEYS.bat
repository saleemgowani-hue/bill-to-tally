@echo off
REM ============================================================
REM  VENDOR TOOL - generate licence keys into an Excel register.
REM  Keep this on YOUR pc. Do not ship it to customers.
REM  SN Softech Solutions  -  9993199719
REM ============================================================

setlocal EnableExtensions
cd /d "%~dp0"
title Generate Licence Keys - SN Softech Solutions
color 0E

if not exist ".venv\Scripts\python.exe" (
    echo  [X] Not installed yet. Run INSTALL.bat first.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo    GENERATE LICENCE KEYS
echo  ============================================================
echo.
set "YEARLY=25"
set "MONTHLY=25"
set /p YEARLY=  How many YEARLY keys?  [25]: 
set /p MONTHLY=  How many MONTHLY keys? [25]: 
if "%YEARLY%"=="" set "YEARLY=25"
if "%MONTHLY%"=="" set "MONTHLY=25"

echo.
".venv\Scripts\python.exe" generate_keys.py --yearly %YEARLY% --monthly %MONTHLY%
if errorlevel 1 (
    echo.
    echo  [X] Key generation failed.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo    Done. Open SN_Softech_Licence_Keys.xlsx
echo.
echo    Keep this file and your .env secret confidential -
echo    anyone with them can create unlimited keys.
echo  ============================================================
echo.
pause
endlocal
