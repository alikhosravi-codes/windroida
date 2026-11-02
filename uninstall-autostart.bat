@echo off
chcp 65001 >nul
title Windroida - remove auto start
set "F=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Windroida-autostart.vbs"
if exist "%F%" del /q "%F%"
if exist "%F%" goto bad
echo.
echo   DONE. Windroida will no longer start with Windows.
echo   انجام شد. دیگه با ویندوز خودکار اجرا نمی‌شه.
echo.
pause
exit /b
:bad
echo.
echo   FAILED to remove: %F%
pause
