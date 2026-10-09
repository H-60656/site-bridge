@echo off
rem Pixiv Bridge / Iwara Bridge / ASMR Bridge one-click launcher (Windows)
rem
rem Usage:
rem   double-click start.bat        open the project navigation, pick one, start it
rem   start.bat iwara               start that API project directly
rem   start.bat asmr_one            start that API project directly (no login)
rem   start.bat --port 9000          use another port
rem   start.bat --host 0.0.0.0       allow other devices on your LAN
rem   start.bat --check              check environment/login only, do not serve
rem   set BRIDGE_SITE=iwara          skip the question, always start iwara
rem
rem The API projects (pixiv/, iwara/, asmr_one/) are siblings: each one has its
rem own run.py and none loads another. This launcher does NOT pick one itself;
rem it hands over to the framework navigation (`python -m pixiv_bridge`), which:
rem   * uses BRIDGE_SITE when set (environment variable, or a BRIDGE_SITE= line
rem     in .env);
rem   * starts the only API project straight away when there is just one;
rem   * lists every API project (and every extension) and asks which one when a
rem     console is available - type the number, Enter = the default (pixiv);
rem   * starts the first one instead when there is no console to ask (piped
rem     stdin), so it never waits forever.
rem
rem To add an extension directory (novel / iwara_web) put a line in .env:
rem   PIXIV_BRIDGE_EXTENSIONS=novel          (for the pixiv project)
rem   PIXIV_BRIDGE_EXTENSIONS=iwara_web      (for the iwara project)
rem (asmr_one takes no extension: it is anonymous-only, see INSTALL.md.)
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

rem ---------- 3. create .env on first run ----------
if not exist ".env" if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo [i] Created .env from .env.example
)

rem ---------- 4. start through the framework's project navigation ----------
rem Which API project runs is decided in ONE place: `python -m pixiv_bridge`
rem (see the header of this file). Nothing here knows about any site directory.
echo [i] Starting through the project navigation...
%PY% -m pixiv_bridge %*

if errorlevel 1 (
    echo.
    echo [x] Failed to start. See INSTALL.md for common causes and fixes.
    pause
)

endlocal
