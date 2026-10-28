@echo off
REM ============================================================
REM  Put an "AI Bill-to-Tally Agent" shortcut on the Desktop.
REM  Run once; after that just double-click the Desktop icon.
REM  SN Softech Solutions  -  9993199719
REM ============================================================

setlocal EnableExtensions
cd /d "%~dp0"
title Create Desktop Shortcut

REM Installed already -> go straight to RUN.bat. Otherwise START.bat,
REM which installs first and then starts the app.
set "TARGET=%~dp0START.bat"
if exist "%~dp0.venv\Scripts\python.exe" set "TARGET=%~dp0RUN.bat"

if not exist "%TARGET%" (
    echo.
    echo  [X] Could not find %TARGET%
    echo      Keep this file inside the extracted app folder.
    echo.
    pause
    exit /b 1
)

set "SC_TARGET=%TARGET%"
set "SC_DIR=%~dp0"
set "SC_NAME=AI Bill-to-Tally Agent"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$desk=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desk ($env:SC_NAME + '.lnk'))); $s.TargetPath=$env:SC_TARGET; $s.WorkingDirectory=$env:SC_DIR.TrimEnd('\'); $s.IconLocation=$env:SystemRoot + '\System32\shell32.dll,13'; $s.Description='AI Bill-to-Tally Agent'; $s.Save(); Write-Host ('Shortcut saved: ' + (Join-Path $desk ($env:SC_NAME + '.lnk')))"

if errorlevel 1 (
    echo.
    echo  [X] Could not create the shortcut.
    echo      Manual way: right-click RUN.bat -^> Send to -^> Desktop ^(create shortcut^)
    echo.
    pause
    exit /b 1
)

echo.
echo  Done. Look for "%SC_NAME%" on your Desktop.
echo  Double-click it to start the software.
echo  ^(Do not move this app folder afterwards - the shortcut points here.
echo   If you move it, run this file again.^)
echo.
pause
