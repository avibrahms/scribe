@echo off
REM Scribe launcher — Windows.
REM
REM Runs scribe_windows.py via pythonw (no console window) from the local
REM venv. Double-click this file to start Scribe, or let the installer's
REM Startup-folder shortcut launch it at login.

cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" (
    echo error: venv not found. Run install.bat first.
    pause
    exit /b 1
)

start "" "venv\Scripts\pythonw.exe" "scribe_windows.py"
