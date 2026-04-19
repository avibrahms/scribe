@echo off
REM Scribe — Windows installer.
REM
REM Does four things:
REM   1. Creates a local venv and pip-installs dependencies.
REM   2. Asks for your Groq API key (if not already set) and writes .env.
REM   3. Creates a Startup-folder shortcut so Scribe auto-starts at login.
REM   4. Launches Scribe so you see the tray icon immediately.
REM
REM Run from inside the cloned repo:
REM     install.bat
REM
REM Re-running is safe — it upserts everything.

setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM --- 1. find Python ------------------------------------------------------

where python >nul 2>nul
if errorlevel 1 (
    echo error: python not found on PATH.
    echo install Python 3.11+ from https://www.python.org/downloads/
    echo IMPORTANT: tick "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

for /f "delims=" %%v in ('python -c "import sys;print(sys.version_info[:2])"') do set PYVER=%%v
echo ==^> using python %PYVER%

REM --- 2. venv + deps ------------------------------------------------------

if not exist "venv\Scripts\python.exe" (
    echo ==^> creating venv
    python -m venv venv
    if errorlevel 1 (
        echo error: venv creation failed.
        pause
        exit /b 1
    )
)

echo ==^> installing dependencies
call "venv\Scripts\python.exe" -m pip install --upgrade pip >nul
call "venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-windows.txt
if errorlevel 1 (
    echo error: pip install failed.
    pause
    exit /b 1
)

REM --- 3. Groq API key -----------------------------------------------------

set NEED_KEY=1
if exist ".env" (
    findstr /b /c:"GROQ_API_KEY=" ".env" >nul 2>nul
    if not errorlevel 1 set NEED_KEY=0
)

if %NEED_KEY%==1 (
    echo.
    echo Scribe uses Groq's free Whisper API for dictation.
    echo Get a free key at https://console.groq.com/keys
    set /p GROQ_KEY="Paste your Groq API key: "
    if "!GROQ_KEY!"=="" (
        echo error: empty key — aborting.
        pause
        exit /b 1
    )
    ^> ".env" echo GROQ_API_KEY=!GROQ_KEY!
    echo ==^> wrote .env
)

REM --- 4. Startup-folder shortcut -----------------------------------------

set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT=%STARTUP%\Scribe.lnk
set TARGET=%~dp0Scribe.bat

echo ==^> installing Startup shortcut at "%SHORTCUT%"
powershell -NoProfile -Command ^
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%');" ^
    "$s.TargetPath='%TARGET%';" ^
    "$s.WorkingDirectory='%~dp0';" ^
    "$s.WindowStyle=7;" ^
    "$s.Description='Scribe — push-to-talk dictation';" ^
    "$s.Save();"

REM --- 5. launch ----------------------------------------------------------

echo.
echo Scribe is installed. Launching now — look for the mic icon in your system tray.
echo (Windows may hide it — click the ^ in the tray to show hidden icons and drag it out.)
echo.
echo If your antivirus flags pynput as a keylogger, whitelist Scribe. pynput only
echo observes key events locally; nothing is sent anywhere except ~3-second audio
echo clips to Groq for transcription.
echo.

start "" /b "Scribe.bat"

echo done.
pause
