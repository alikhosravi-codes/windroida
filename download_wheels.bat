@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Run this ONCE on a PC that has full internet. It saves the libraries into the "wheels" folder.
rem Copy the whole Windroida folder to the server: start.bat then installs from "wheels" without internet.
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY set PY=python
%PY% -m pip download -r requirements.txt -d wheels
echo.
echo Done. The "wheels" folder now contains everything for offline install.
pause
