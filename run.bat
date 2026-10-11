@echo off
rem RTL-SDR Player launcher — keeps the window open so errors stay readable.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python was not found on this PC.
  echo  Install it from https://www.python.org/downloads/ and tick
  echo  "Add python.exe to PATH" during setup, then run setup.bat once.
  echo.
  pause
  exit /b 1
)

python app.py %*

echo.
echo Server stopped. If it exited by itself right after starting,
echo read the message above - it names the exact problem.
pause
