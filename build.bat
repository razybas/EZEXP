@echo off
rem Builds EzExp into a single portable exe at dist\ezexp.exe
cd /d "%~dp0"
py -m PyInstaller --noconfirm --onefile --windowed --name ezexp app.py
echo.
echo Done. Your exe is at: %~dp0dist\ezexp.exe
pause
