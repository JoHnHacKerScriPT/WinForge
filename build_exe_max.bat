@echo off
setlocal

echo.
echo === Checking Python ===
py -3 --version
if errorlevel 1 goto no_python

echo.
echo === Installing build dependencies - this can take a few minutes ===
py -3 -m pip install --upgrade pip --break-system-packages 2>nul
py -3 -m pip install --upgrade pyinstaller pyside6 psutil
if errorlevel 1 goto pip_failed

echo.
echo === Building WinForge.exe ===
py -3 -m PyInstaller --onefile --windowed --uac-admin --name "WinForge" --icon "app_icon.ico" --add-data "app_icon.ico;." --add-data "app_icon.png;." --add-data "page_icons;page_icons" --add-data "action_icons;action_icons" --add-data "dashboard_icons;dashboard_icons" --add-data "status_icons;status_icons" --add-data "wu_icons;wu_icons" --version-file "version_info.txt" --clean wintoys_like_full_plus_max.py
if errorlevel 1 goto build_failed

echo.
echo ============================================================
echo   DONE! Your standalone app is here:
echo   dist\WinForge.exe
echo.
echo   You can copy JUST that one .exe file to any Windows PC and
echo   run it directly - no Python installation needed there.
echo ============================================================
echo.
pause
goto :eof

:no_python
echo.
echo [ERROR] Python was not found.
echo Install it from https://www.python.org/downloads/
echo During setup, tick the box "Add python.exe to PATH".
echo Then run this file again.
pause
exit /b 1

:pip_failed
echo.
echo [ERROR] pip install failed. Check your internet connection and try again.
pause
exit /b 1

:build_failed
echo.
echo [ERROR] Build failed - scroll up to see what PyInstaller reported.
pause
exit /b 1
