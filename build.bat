@echo off
rem Builds EzExp into a single portable exe at dist\ezexp.exe
rem Requires Python 3.10+ on PATH (get it from https://python.org, tick "Add to PATH").
cd /d "%~dp0"

where py >nul 2>nul && (set "PY=py") || (set "PY=python")
echo Using Python launcher: %PY%
%PY% --version || (echo Python was not found. Install it from https://python.org first. & pause & exit /b 1)

echo Installing dependencies (PySide6, PyInstaller)...
%PY% -m pip install --quiet PySide6 pyinstaller || (echo pip install failed. & pause & exit /b 1)

echo Building...
%PY% -m PyInstaller --noconfirm --onefile --windowed --name ezexp app.py || (echo Build failed. & pause & exit /b 1)

echo.
echo Done. Your exe is at: %~dp0dist\ezexp.exe
pause
