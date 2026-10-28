@echo off
REM ============================================================
REM  Optional add-on: free/offline OCR engines.
REM  Run this ONLY if you want Tesseract and/or PaddleOCR instead
REM  of a paid AI vision engine (Gemini/OpenAI/Claude).
REM
REM  IMPORTANT - what this does NOT do:
REM   - Tesseract and PaddleOCR read plain text only. They work
REM     reasonably for Purchase/Sales bill photos (lower accuracy
REM     than an AI vision engine), but they CANNOT read a Bank
REM     statement's table layout at all. For Bank statement
REM     photos/PDFs you still need an AI vision engine, or import
REM     the statement as Excel/CSV instead.
REM   - This adds ~300-600 MB of downloads (mostly PaddlePaddle).
REM     Run it on a good connection.
REM
REM  SN Softech Solutions  -  9993199719
REM ============================================================

setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Offline OCR setup - AI Bill-to-Tally Agent
color 0B

echo.
echo  ============================================================
echo    INSTALLING FREE / OFFLINE OCR (Tesseract + PaddleOCR)
echo  ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo  [X] No .venv found. Run INSTALL.bat or START.bat first,
    echo      then run this script.
    pause
    exit /b 1
)
set "VPY=.venv\Scripts\python.exe"

echo  [1/3] Installing the Python side (pytesseract + PaddleOCR)...
echo        This is the ~300-600 MB part - please be patient.
"%VPY%" -m pip install --quiet pytesseract
if errorlevel 1 (
    echo  [!] Could not install pytesseract. Check the internet connection.
) else (
    echo  [OK] pytesseract installed.
)

REM Pin paddlepaddle below 3.3 - 3.3.0/3.3.1 have a known CPU oneDNN bug
REM (NotImplementedError: ConvertPirAttribute2RuntimeAttribute...) that
REM breaks PP-StructureV3 inference. See PaddlePaddle/Paddle#77340.
"%VPY%" -m pip install --quiet paddleocr "paddlepaddle==3.2.2"
if errorlevel 1 (
    echo        Default index had no wheel for your Python - trying
    echo        PaddlePaddle's own package index instead...
    "%VPY%" -m pip install --quiet "paddlepaddle==3.2.2" -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
    "%VPY%" -m pip install --quiet paddleocr
)
if errorlevel 1 (
    echo  [!] Could not install PaddleOCR/PaddlePaddle. This usually means
    echo      your Python version is too new/old for PaddlePaddle's
    echo      published wheels. Tesseract below will still be set up -
    echo      it alone is enough for offline OCR. You can retry PaddleOCR
    echo      later, or ask SN Softech Solutions for the supported
    echo      Python version.
) else (
    echo  [OK] PaddleOCR installed. Its language models download the
    echo       first time you actually use it - that needs internet once.
)

echo.
echo  [1b/3] PP-StructureV3 extras (table-aware reading, optional)...
echo         Only useful if you picked "PP-StructureV3" or "Auto" as the
echo         engine in Settings. Skip this if plain PaddleOCR above is
echo         enough for you - it's a bigger download (~1-2 GB total with
echo         its models, which download the first time it actually runs).
set /p PPSTRUCTURE_YN="Install PP-StructureV3 extras now? (y/N): "
if /i "%PPSTRUCTURE_YN%"=="y" (
    REM PaddleX gates the PP-StructureV3 pipeline behind its own extra -
REM paddleocr[doc-parser] alone is not enough, so install both.
    "%VPY%" -m pip install --quiet "paddleocr[doc-parser]" "paddlex[ocr]" "paddlepaddle==3.2.2"
    if errorlevel 1 (
        echo  [!] Could not install the PP-StructureV3 extras. Plain
        echo      PaddleOCR/Tesseract above still work. You can retry this
        echo      script later, or run by hand:
        echo        .venv\Scripts\python.exe -m pip install "paddleocr[doc-parser]" "paddlex[ocr]"
    ) else (
        echo  [OK] PP-StructureV3 extras installed. Select it in Settings
        echo       -^> AI / OCR. The first bill after each app start will be
        echo       slow while its models load into memory.
    )
) else (
    echo        Skipped.
)

REM RapidOCR (the default engine) and Tesseract (the fallback) are both
REM already installed by INSTALL.bat/START.bat - nothing to do for them
REM here. This script is only for the heavier, optional engines below.

echo.
echo  [2/3] Checking for the Tesseract program...
set "TESS_FOUND="
where tesseract >nul 2>&1 && set "TESS_FOUND=1"
if exist "%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe" (
    set "TESS_FOUND=1"
    set "TESS_PATH=%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"
)
if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" (
    set "TESS_FOUND=1"
    set "TESS_PATH=%ProgramFiles%\Tesseract-OCR\tesseract.exe"
)

if defined TESS_FOUND (
    echo  [OK] Tesseract is already installed.
) else (
    echo        Not found - downloading the official Windows installer
    echo        from UB-Mannheim ^(the standard Tesseract build for
    echo        Windows^). This is a one-time ~80 MB download.
    set "TESS_URL=https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/tesseract-ocr-w64-setup-5.5.3.20260724.exe"
    set "TESS_TEMP=%TEMP%\tesseract-setup.exe"
    set "TESS_DIR=%LOCALAPPDATA%\Tesseract-OCR"
    if exist "%TESS_TEMP%" del /f /q "%TESS_TEMP%" >nul 2>&1

    where curl >nul 2>&1
    if not errorlevel 1 (
        echo        Trying curl...
        curl -L --fail --ssl-no-revoke -o "%TESS_TEMP%" "%TESS_URL%"
    )
    if not exist "%TESS_TEMP%" (
        echo        Trying PowerShell...
        powershell -NoProfile -Command ^
            "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri '%TESS_URL%' -OutFile '%TESS_TEMP%' -UseBasicParsing } catch { Write-Host $_.Exception.Message; exit 1 }"
    )
    if not exist "%TESS_TEMP%" (
        echo  [!] Could not download Tesseract automatically. This is most
        echo      often a firewall/antivirus/proxy blocking the download,
        echo      not a problem with this app. Install it by hand instead -
        echo      it only takes a minute:
        echo        1. Open https://github.com/UB-Mannheim/tesseract/wiki
        echo        2. Download the 64-bit installer and run it (Next,
        echo           Next, Install - defaults are fine)
        echo        3. Then paste the install path into this app's
        echo           Settings -^> AI / OCR -^> "Tesseract executable path"
        echo           (usually C:\Program Files\Tesseract-OCR\tesseract.exe)
        goto :tesseract_done
    )

    echo        Installing to %TESS_DIR% ^(no admin rights needed^)...
    "%TESS_TEMP%" /S /D=%TESS_DIR%
    REM The installer runs its own background process; give it a moment.
    timeout /t 8 /nobreak >nul

    if exist "%TESS_DIR%\tesseract.exe" (
        set "TESS_PATH=%TESS_DIR%\tesseract.exe"
        echo  [OK] Tesseract installed to %TESS_DIR%
    ) else (
        echo  [!] The installer ran but tesseract.exe was not found where
        echo      expected. It may still be mid-install - wait a minute
        echo      and check %TESS_DIR%, or install by hand from
        echo      https://github.com/UB-Mannheim/tesseract/wiki
    )
)
:tesseract_done

echo.
echo  [3/3] Done.
echo  ============================================================
if defined TESS_PATH (
    echo    Tesseract path to paste into Settings:
    echo      %TESS_PATH%
    echo.
    echo    In the app: Settings -^> AI / OCR -^> select "Tesseract"
    echo    -^> paste that path into "Tesseract executable path" -^>
    echo    Save AI settings.
) else (
    echo    Tesseract path not confirmed - see the notes above.
)
echo.
echo    PaddleOCR needs no path - just select "PaddleOCR" as the engine
echo    in Settings -^> AI / OCR and Save. (RapidOCR, the default engine,
echo    and Tesseract were already set up by INSTALL.bat/START.bat.)
echo.
echo    Reminder: none of these engines can read Bank statement photos/PDFs
echo    ^(table layout^) - use Excel/CSV there, or an AI vision engine.
echo  ============================================================
echo.
pause
