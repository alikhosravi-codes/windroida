@echo off
chcp 65001 >nul
title Windroida
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY goto nopython
%PY% start.py %*
if errorlevel 1 pause
exit /b
:nopython
echo.
echo   Python is not installed.
echo   Download it from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" in the installer, then run start.bat again.
echo.
pause
