@echo off
cd /d "%~dp0"
title Canopy Seed
echo.
echo   Starting Canopy Seed...
echo   Your browser will open automatically.
echo   Press Ctrl+C to stop the server.
echo.
call "%~dp0.venv\Scripts\activate.bat"
python "%~dp0start.py"
pause
