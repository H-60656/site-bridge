@echo off
rem Pixiv Bridge / Iwara Bridge one-click launcher (Windows)
rem
rem Usage:
rem   double-click start.bat        start the API project found in this folder
rem   start.bat --port 9000          use another port
rem   start.bat --host 0.0.0.0       allow other devices on your LAN
rem   start.bat --check              check environment/login only, do not serve
rem   set BRIDGE_SITE=iwara          pick the API project explicitly
rem
rem The two API projects pixiv/ and iwara/ are siblings: each one has its own
rem run.py and neither loads the other. This launcher starts ONE of them:
rem   * BRIDGE_SITE=pixiv / iwara when set
rem   * otherwise the first of pixiv\run.py, iwara\run.py that exists
rem
rem To add an extension directory (novel / iwara_web) put a line in .env:
rem   PIXIV_BRIDGE_EXTENSIONS=novel          (for the pixiv project)
rem   PIXIV_BRIDGE_EXTENSIONS=iwara_web      (for the iwara project)
rem The launcher finds <extension>/extension.py automatically; extensions have
rem no entry script of their own (see README, section about extensions).
rem
rem The first run installs dependencies with pip (fastapi / uvicorn / httpx);
rem it takes 1-3 minutes. Chinese walkthrough: see INSTALL.md.
rem
rem NOTE: this file is intentionally ASCII-only. A .bat file containing Chinese
rem text is mis-decoded by cmd.exe (codepage 936 console), which breaks parsing.
rem The server itself prints Chinese fine because Python uses the console API.

setlocal
cd /d "%~dp0"

rem ---------- 1. locate Python ----------
set "PY=python"
where python >nul 2>nul
if errorlevel 1 set "PY=py"

where %PY% >nul 2>nul
if errorlevel 1 (
    echo [x] Python not found. Please install Python 3.10 or newer first:
    echo     1^) open https://www.python.org/downloads/windows/
    echo     2^) download "Windows installer ^(64-bit^)" and run it
    echo     3^) on the first screen, tick "Add python.exe to PATH"
    echo     Then close this window and double-click start.bat again.
    echo     Chinese step-by-step guide: INSTALL.md
    pause
    exit /b 1
)

rem ---------- 2. make sure this python actually runs ----------
rem Windows ships an "app execution alias" that pretends to be python.exe
rem and just opens the Microsoft Store when executed.
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo [x] Found %PY%, but it cannot run.
    echo     If it is the Windows "app execution alias", turn it off in:
    echo       Settings -^> Apps -^> Advanced app settings -^> App execution aliases
    echo     and disable python.exe / python3.exe, or install Python from python.org.
    echo     Chinese guide: INSTALL.md
    pause
    exit /b 1
)

rem ---------- 3. pick the API project (pixiv and iwara are siblings) ----------
set "SITE=%BRIDGE_SITE%"
if not defined SITE if exist "pixiv\run.py" set "SITE=pixiv"
if not defined SITE if exist "iwara\run.py" set "SITE=iwara"

if not defined SITE (
    echo [x] No API project found here: expected pixiv\run.py or iwara\run.py.
    echo     Start this launcher from the folder that contains them.
    pause
    exit /b 1
)

set "ENTRY=%SITE%\run.py"
if not exist "%ENTRY%" (
    echo [x] API project "%SITE%" not found: %ENTRY% is missing.
    pause
    exit /b 1
)

rem ---------- 4. create .env on first run ----------
if not exist ".env" if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo [i] Created .env from .env.example
)

rem ---------- 5. start (missing dependencies are installed automatically) ----------
echo [i] Starting API project: %SITE% ^(%ENTRY%^)
echo [i] The first run downloads dependencies, please wait 1-3 minutes...
%PY% "%ENTRY%" %*

if errorlevel 1 (
    echo.
    echo [x] Failed to start. See INSTALL.md for common causes and fixes.
    pause
)

endlocal
