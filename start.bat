@echo off
setlocal
cd /d "%~dp0"
title Avenro
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopy
%PY% -c "import sys,sqlite3;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>nul || goto nopy
echo Starting Avenro...
%PY% server.py --open %*
if errorlevel 1 pause
exit /b 0
:nopy
echo.
echo Python 3.9 or newer was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run start.bat again.
pause
exit /b 1
