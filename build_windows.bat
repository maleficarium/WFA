@echo off
rem Build WoWAddonUpdater.exe on Windows 10/11. Needs Python 3.10+ from python.org on PATH.
cd /d "%~dp0"
py -3 -m pip install --upgrade pyinstaller || goto :fail
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name WoWAddonUpdater addon_updater.py || goto :fail
echo.
echo Done: dist\WoWAddonUpdater.exe
pause
exit /b 0
:fail
echo Build failed.
pause
exit /b 1
