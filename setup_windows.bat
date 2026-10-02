@echo off
setlocal
cd /d "%~dp0"
title Canvas Morning Brief - Setup
echo.
echo  ===============================================
echo    Canvas Morning Brief - one-time setup
echo  ===============================================
echo.

echo %~dp0 | find /i "\Downloads\" >nul
if not errorlevel 1 (
  echo  NOTE: This folder is inside Downloads. The daily startup will run from
  echo  wherever this folder is now. It's better to move it somewhere permanent
  echo  first, like Documents\Canvas-Bot, then run this setup again from there.
  echo.
  choice /c YN /m "  Continue setup from Downloads anyway"
  if errorlevel 2 exit /b
  echo.
)

where python >nul 2>nul
if errorlevel 1 (
  echo  Python isn't installed or isn't on PATH.
  echo  Install it from python.org/downloads and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

echo  [1/6] Installing add-ons...
python -m pip install -r requirements.txt
if errorlevel 1 goto fail
python -m playwright install chromium
if errorlevel 1 goto fail

echo.
echo  [2/6] Your Canvas address and timezone
if not exist .env copy .env.example .env >nul
echo  Notepad will open. Set CANVAS_BASE_URL to the address you use for Canvas
echo  (copy it from your browser, e.g. https://something.instructure.com).
echo  Save it when you are done.
start "" notepad .env
echo.
echo  Press any key here AFTER you have saved .env...
pause >nul

echo.
echo  [3/6] Your settings (WeBWorK + Labflow links, hobbies, quotes)
if not exist my_settings.json copy my_settings.example.json my_settings.json >nul
echo  Notepad will open. Replace the two PASTE lines with the Canvas links you
echo  click to open WeBWorK and Labflow (right-click each link in Canvas, then
echo  "Copy link address"). Save it when you are done.
start "" notepad my_settings.json
echo.
echo  Press any key here AFTER you have saved my_settings.json...
pause >nul

echo.
echo  [4/6] Log in to Canvas once
python morning_brief.py --login
if errorlevel 1 goto fail

echo.
echo  [5/6] Run automatically every time you log in to Windows
python morning_brief.py --install-startup
if errorlevel 1 goto fail

echo.
echo  [6/6] Building your first brief...
where claude >nul 2>nul
if errorlevel 1 (
  echo  Tip: for the AI "Do today" plan, install Claude Code later. See the README.
)
python morning_brief.py --dry-run
if errorlevel 1 goto fail

echo.
echo  All set! Your dashboard should be open in your browser.
echo  From now on it updates the first time you log in each day.
echo  Use "Refresh Morning Brief" on your Desktop to update it anytime.
echo.
pause
exit /b 0

:fail
echo.
echo  Something went wrong in the step above. Copy the error text and send it to Claude.
pause
exit /b 1
