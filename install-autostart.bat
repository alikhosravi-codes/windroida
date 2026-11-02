@echo off
chcp 65001 >nul
title Windroida - install auto start
set "SD=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "F=%SD%\Windroida-autostart.vbs"
echo Set s = CreateObject("WScript.Shell")> "%F%"
echo WScript.Sleep 20000>> "%F%"
echo s.CurrentDirectory = "%~dp0">> "%F%"
echo s.Run "cmd /c ""%~dp0start.bat"" --no-browser", 7, False>> "%F%"
if exist "%F%" goto ok
echo.
echo   FAILED. The file could not be created.
pause
exit /b 1
:ok
echo.
echo   DONE. Windroida will start by itself (minimized) 20 seconds after Windows starts.
echo   Cancel it any time: run  uninstall-autostart.bat
echo.
echo   انجام شد. از این به بعد با روشن شدن ویندوز، سرور خودش (کوچک‌شده) بالا میاد.
echo   برای لغو: uninstall-autostart.bat رو اجرا کن.
echo.
pause
