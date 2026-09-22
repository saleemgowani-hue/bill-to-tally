@echo off
REM ============================================================
REM  Rebuild the DEMO company from scratch.
REM  Deletes every demo bill, product and stock figure, then
REM  seeds it again. No other company is touched.
REM  SN Softech Solutions  -  9993199719
REM ============================================================

setlocal EnableExtensions
cd /d "%~dp0"
title Reset Demo Data - AI Bill-to-Tally Agent
color 0E

if not exist ".venv\Scripts\python.exe" (
    echo  [X] Not installed yet. Run INSTALL.bat first.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo    RESET DEMO DATA
echo  ============================================================
echo.
echo    This DELETES all demo bills, products and stock figures
echo    and builds the demo company again.
echo.
echo    Close the app first if it is running.
echo    Other companies are NOT affected.
echo.
echo    1  Rebuild with opening stock ^(120, 300, 40, 80, 60^)
echo    2  Rebuild with stock 0 for every product
echo    3  Rebuild with no products at all
echo    4  Cancel
echo.
set "PICK=4"
set /p PICK=  Choose 1-4 [4]: 

set "FLAGS="
if "%PICK%"=="1" set "FLAGS=--reset"
if "%PICK%"=="2" set "FLAGS=--reset --empty"
if "%PICK%"=="3" set "FLAGS=--reset --no-products"
if "%PICK%"=="4" goto :cancelled
if not defined FLAGS goto :cancelled

echo.
set "SURE=n"
set /p SURE=  Type Y to confirm deletion: 
if /i not "%SURE%"=="Y" goto :cancelled

echo.
".venv\Scripts\python.exe" seed_demo.py %FLAGS%
if errorlevel 1 (
    echo.
    echo  [X] Reset failed. If the app is running, close it and try again.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo    Done. Start the app with START.bat
echo  ============================================================
echo.
pause
exit /b 0

:cancelled
echo.
echo  Cancelled. Nothing was changed.
echo.
pause
endlocal
