@echo off
rem Builds EzExp into a single portable exe at dist\ezexp.exe
rem Needs Python 3.10+. If none is found, it installs Python via
rem winget (built into Windows 10/11) and then builds.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

call :find_python
if defined PY goto :have_python

echo Python was not found - installing it via winget...
where winget >nul 2>nul || (echo winget is not available on this PC. Install Python from https://python.org, tick "Add to PATH", then run build.bat again. & pause & exit /b 1)
winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
call :find_python
if not defined PY (echo Python installation did not complete. Install it from https://python.org, tick "Add to PATH", then run build.bat again. & pause & exit /b 1)

:have_python
echo Using Python: %PY%
%PY% --version

echo Installing dependencies (PySide6, PyInstaller, Pillow)...
%PY% -m pip install --quiet PySide6 pyinstaller pillow || (echo pip install failed. & pause & exit /b 1)

echo Generating icon...
%PY% make_icon.py || (echo Icon generation failed. & pause & exit /b 1)

echo Building...
%PY% -m PyInstaller --noconfirm --onefile --windowed --name ezexp --icon ezexp.ico --version-file version_info.txt app.py || (echo Build failed. & pause & exit /b 1)

echo.
echo Done. Your exe is at: %~dp0dist\ezexp.exe
pause
exit /b 0

:find_python
rem Sets PY to a VERIFIED working Python (the bare "python" name can
rem be the Microsoft Store stub, so every candidate must answer
rem --version), or leaves PY undefined.
set "PY="
py --version >nul 2>nul && (set "PY=py" & exit /b 0)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" ("%%D\python.exe" --version >nul 2>nul && (set "PY=%%D\python.exe" & exit /b 0))
for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-3*-64") do if exist "%%D\python.exe" ("%%D\python.exe" --version >nul 2>nul && (set "PY=%%D\python.exe" & exit /b 0))
python --version >nul 2>nul && (set "PY=python" & exit /b 0)
exit /b 0
