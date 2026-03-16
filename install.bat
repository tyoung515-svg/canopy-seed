@echo off
REM ──────────────────────────────────────────────────────────────
REM  Canopy Seed — Installer (Windows)
REM  Double-click or run: install.bat
REM ──────────────────────────────────────────────────────────────

REM ── Fix working directory (handles double-click from any location) ──
cd /d "%~dp0"

REM ── Unblock files flagged by SmartScreen/Windows download quarantine ──
powershell -Command "Get-ChildItem '%~dp0' -Recurse -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

REM ── Self-elevate to admin if not already ─────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

setlocal EnableDelayedExpansion
title Canopy Seed — Installer

echo.
echo   ===========================================
echo    Canopy Seed ^| Installer
echo   ===========================================
echo.

REM ── 1. Find a real Python (not the Windows Store stub) ───────
set PYTHON_CMD=

REM Try the Windows Python Launcher first — most reliable, skips store stubs
where py >nul 2>&1
if not errorlevel 1 (
    py -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set PYTHON_CMD=py
    )
)

REM Fall back to python, but verify it isn't the store stub
if not defined PYTHON_CMD (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys" >nul 2>&1
        if not errorlevel 1 (
            set PYTHON_CMD=python
        )
    )
)

if not defined PYTHON_CMD (
    echo   [FAIL] Python not found or only the Microsoft Store stub is installed.
    echo.
    echo   Please install Python 3.11+ from https://python.org
    echo   During install, check "Add Python to PATH".
    echo.
    echo   If Python is already installed, go to:
    echo   Settings ^> Apps ^> Advanced app settings ^> App execution aliases
    echo   and turn OFF the Python aliases.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('%PYTHON_CMD% --version 2^>^&1') do set PY_VER=%%v
echo   [OK] Python %PY_VER% found

REM ── 2. Create virtual environment ────────────────────────────
if not exist ".venv\" (
    echo   Creating virtual environment...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo   [FAIL] Could not create virtual environment.
        pause
        exit /b 1
    )
    echo   [OK] Virtual environment created
) else (
    echo   [OK] Virtual environment already exists
)

REM ── 3. Install dependencies ───────────────────────────────────
echo   Installing dependencies ^(this may take a minute^)...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo   [FAIL] Dependency installation failed.
    echo   Try running: pip install -r requirements.txt
    pause
    exit /b 1
)
echo   [OK] Dependencies installed

REM ── 3b. Install Playwright Chromium (needed for Manager screenshots) ──────
echo   Installing Playwright Chromium browser ^(~150 MB, one-time^)...
python -m playwright install chromium --with-deps >nul 2>&1
if errorlevel 1 (
    echo   [WARN] Playwright browser install failed — Manager screenshots disabled.
    echo   You can retry later: python -m playwright install chromium
) else (
    echo   [OK] Playwright Chromium installed
)

REM ── 4. Create .env (vault mode on by default — no API keys here) ──────────
if not exist ".env" (
    copy .env.example .env >nul
    echo   [OK] Created .env ^(vault mode enabled -- keys entered on first launch^)
) else (
    echo   [OK] .env already exists
)

REM ── 5. Create required directories ───────────────────────────
if not exist "exports" mkdir exports
if not exist "logs" mkdir logs
if not exist "memory\sessions" mkdir memory\sessions
if not exist "outputs" mkdir outputs
echo   [OK] Directories ready

REM ── 6. Create launch script ──────────────────────────────────
(
    echo @echo off
    echo cd /d "%%~dp0"
    echo title Canopy Seed
    echo echo.
    echo echo   Starting Canopy Seed...
    echo echo   Your browser will open automatically.
    echo echo   Press Ctrl+C to stop the server.
    echo echo.
    echo call "%%~dp0.venv\Scripts\activate.bat"
    echo python "%%~dp0start.py"
    echo pause
) > "START CANOPY.bat"
echo   [OK] Launch script created: START CANOPY.bat

REM ── Done ──────────────────────────────────────────────────────
echo.
echo   ===========================================
echo    Canopy Seed is ready to plant seeds.
echo   ===========================================
echo.
echo   Next steps:
echo   1. Double-click "START CANOPY.bat" to launch.
echo      Your browser will open to Canopy Seed automatically.
echo.
echo   2. Using Claude or Gemini? Click "Vault Setup" in the DevHub
echo      to securely store your API key ^(AES-encrypted, never in .env^).
echo      Using LM Studio or Ollama? No key needed -- you're ready to go.
echo.
echo   3. URLs:
echo      Main:  http://localhost:7822
echo      Hub:   http://localhost:7822/hub
echo.
pause
