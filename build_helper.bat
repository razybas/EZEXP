@echo off
rem Builds the EzExp USN helper into dist\EzExpUsnHelper.exe (onefile,
rem console — the service needs stdout for sc.exe diagnostics).
rem The helper is stdlib + ezexp.usn_journal/ezexp.usn_proto only, so the
rem exe is far smaller than the app's (no PySide6, no Pillow).
setlocal EnableDelayedExpansion
cd /d "%~dp0"

call :find_python
if not defined PY (echo Python was not found. Run build.bat once first. & pause & exit /b 1)
echo Using Python: %PY%

echo Installing PyInstaller...
%PY% -m pip install --quiet pyinstaller || (echo pip install failed. & pause & exit /b 1)

echo Building...
%PY% -m PyInstaller --noconfirm --onefile --console --name EzExpUsnHelper usn_helper_main.py || (echo Build failed. & pause & exit /b 1)

echo.
echo Done. The helper exe is at: %~dp0dist\EzExpUsnHelper.exe
echo Install it later from an ELEVATED prompt:  %~dp0dist\EzExpUsnHelper.exe --install
pause
exit /b 0

:find_python
rem Same verified-Python logic as build.bat.
set "PY="
py --version >nul 2>nul && (set "PY=py" & exit /b 0)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" ("%%D\python.exe" --version >nul 2>nul && (set "PY=%%D\python.exe" & exit /b 0))
for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-3*-64") do if exist "%%D\python.exe" ("%%D\python.exe" --version >nul 2>nul && (set "PY=%%D\python.exe" & exit /b 0))
python --version >nul 2>nul && (set "PY=python" & exit /b 0)
exit /b 0
