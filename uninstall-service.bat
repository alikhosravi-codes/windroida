@echo off
chcp 65001 >nul
title Windroida - remove background service
net session >nul 2>&1
if errorlevel 1 goto noadmin
schtasks /end /tn "Windroida" >nul 2>nul
schtasks /delete /tn "Windroida" /f >nul 2>nul
netsh advfirewall firewall delete rule name="Windroida" >nul 2>nul
del /q "%~dp0service-run.bat" >nul 2>nul
echo.
echo   DONE. Windroida will no longer start at boot.
echo   (If it is running right now, close its python process in Task Manager or restart the PC.)
echo   انجام شد. دیگه با روشن شدن کامپیوتر خودکار اجرا نمی‌شه.
echo.
pause
exit /b 0
:noadmin
echo   Run as administrator: right-click this file ^> Run as administrator.
pause
exit /b 1
