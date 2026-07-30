@echo off
REM Builds NowPlayingBridge.exe into the dist\ folder.
REM Run this on Windows, in this folder, with Python 3.10+ installed.

echo.
echo === NowPlaying Bridge - build ===
echo.

python --version >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install it from python.org and tick
  echo "Add python.exe to PATH" during setup, then run this again.
  pause
  exit /b 1
)

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller pillow
if errorlevel 1 (
  echo.
  echo Dependency install failed. Scroll up for the reason.
  pause
  exit /b 1
)

REM Rebuild the .ico from the PNG master. Pillow's container is known-good; a
REM hand-written one can be odd enough that PyInstaller silently falls back to
REM the default Python icon.
echo Preparing the icon...
python -c "from PIL import Image; im=Image.open('nowplaying-bridge.png').convert('RGBA'); im.save('nowplaying-bridge.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)]); print('icon ready')"

echo.
echo Building the exe...
python -m PyInstaller --onefile --windowed --clean --noconfirm ^
  --name NowPlayingBridge ^
  --icon nowplaying-bridge.ico ^
  --version-file version_info.txt ^
  --collect-all winsdk ^
  --collect-all winotify ^
  --hidden-import status_window ^
  --add-data "status_window.py;." ^
  --add-data "nowplaying-bridge.ico;." ^
  nowplaying_bridge.py
if errorlevel 1 (
  echo.
  echo Build failed. Scroll up for the reason.
  pause
  exit /b 1
)

echo.
echo Done. Your app is at:  dist\NowPlayingBridge.exe
echo Double-click it to test, then upload that one file to a GitHub release.
echo.
pause
