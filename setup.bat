@echo off
setlocal
cd /d "%~dp0"
title Avenro setup
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (echo Python was not found. Install it from https://www.python.org/downloads/ & pause & exit /b 1)
%PY% setup_env.py
pause
