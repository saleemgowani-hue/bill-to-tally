@echo off
REM ============================================================
REM  Copy the database and uploaded bill images into backups\
REM  Run this daily. Keep a copy off this PC.
REM  SN Softech Solutions  -  9993199719
REM ============================================================

setlocal EnableExtensions
cd /d "%~dp0"
title Backup - AI Bill-to-Tally Agent
color 0A

if not exist "data" (
    echo  [X] No data folder found - nothing to back up.
    pause
    exit /b 1
)

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set "STAMP=%%i"
set "DEST=backups\%STAMP%"

echo.
echo  Backing up to %DEST% ...
mkdir "%DEST%" 2>nul

if exist "data\billtotally.db" copy /Y "data\billtotally.db" "%DEST%\" >nul
if exist ".env" copy /Y ".env" "%DEST%\" >nul
if exist "data\uploads" xcopy /E /I /Q /Y "data\uploads" "%DEST%\uploads" >nul

echo.
echo  Backup complete: %DEST%
echo.
echo  Copy this folder to a pen drive or cloud drive as well -
echo  a backup on the same PC does not protect against disk failure.
echo.
pause
endlocal
