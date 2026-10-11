@echo off
REM ============================================================
REM  RTL-SDR Player - one-time setup for Windows 10 / 11
REM ============================================================
setlocal
cd /d "%~dp0"

echo.
echo   RTL-SDR Player setup
echo   --------------------
echo.

REM --- 1. Python -------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [X] Python was not found. Install Python 3.10+ from
    echo     https://www.python.org/downloads/  and tick "Add to PATH",
    echo     then run this script again.
    goto :end
)
echo [ok] Python found.

REM --- 2. Dependencies --------------------------------------------------
echo [*] Installing Python packages (fastapi, uvicorn, numpy, scipy, pyrtlsdr)...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [X] pip install failed - check your internet connection and retry.
    goto :end
)
echo [ok] Python packages installed.

REM --- 3. Native librtlsdr DLLs -----------------------------------------
if exist rtlsdr.dll (
    echo [ok] rtlsdr.dll found in this folder.
) else (
    echo.
    echo [!] Native DLLs missing - two small files are needed:
    echo.
    echo     1. Go to:  https://github.com/librtlsdr/librtlsdr/releases
    echo        download the newest  librtlsdr-win32-x64  zip  ^(or
    echo        rel_with_dlls from the RTL-SDR Blog driver release^).
    echo     2. Copy  rtlsdr.dll  and  libusb-1.0.dll  into this folder:
    echo        %cd%
    echo.
)

REM --- 4. USB driver ------------------------------------------------------
echo.
echo [!] USB driver check (one-time):
echo     If the app later reports "Could not open the RTL2832U":
echo       1. Download Zadig:  https://zadig.akeo.ie
echo       2. Plug in the dongle, run Zadig
echo       3. Options ^> "List All Devices" ^> pick
echo          "Bulk-In, Interface (Interface 0)"
echo       4. Ensure target driver is WinUSB, click "Replace Driver"
echo     ^(this replaces the default DVB-T driver and takes ~5 seconds^)
echo.

:end
echo Setup finished. Launch the player with:   run.bat
pause
