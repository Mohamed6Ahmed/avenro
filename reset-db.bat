@echo off
cd /d "%~dp0"
echo This deletes db\avenro.db (accounts, orders, subscribers) and rebuilds it from db\seed.sql.
set /p OK=Type YES to continue: 
if /i not "%OK%"=="YES" exit /b 0
if exist db\avenro.db del db\avenro.db
call start.bat
