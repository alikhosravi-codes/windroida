@echo off
chcp 65001 >nul
title Windroida - install background service
net session >nul 2>&1
if errorlevel 1 goto noadmin
cd /d "%~dp0"
set "PY="
for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
if not defined PY goto nopy
echo   Python: %PY%
echo   Checking libraries for the background account (may download a little)...
"%PY%" -c "import cryptography" >nul 2>nul || "%PY%" -m pip install --disable-pip-version-check -q cryptography
> "%~dp0service-run.bat" echo @echo off
>> "%~dp0service-run.bat" echo cd /d "%~dp0"
>> "%~dp0service-run.bat" echo "%PY%" start.py --no-browser --no-datasheet
schtasks /create /tn "Windroida" /tr "\"%~dp0service-run.bat\"" /sc onstart /delay 0000:30 /ru SYSTEM /rl HIGHEST /f
if errorlevel 1 goto fail
netsh advfirewall firewall delete rule name="Windroida" >nul 2>nul
netsh advfirewall firewall add rule name="Windroida" dir=in action=allow protocol=TCP localport=8081,8765,8082,8084 >nul
schtasks /run /tn "Windroida" >nul
echo.
echo   DONE. Windroida now starts when the PC boots, even before anyone logs in to Windows.
echo   It runs hidden in the background, with no windows. The admin datasheet is not started in this mode:
echo   open it yourself after logging in with   start.bat --no-browser   (or just start.bat).
echo   Cancel: run  uninstall-service.bat
echo.
echo   انجام شد. از این به بعد با روشن شدن کامپیوتر، حتی بدون وارد شدن به ویندوز، سرور بالا میاد.
echo   پنجره‌ای نشون داده نمی‌شه. datasheet ادمین توی این حالت خودکار باز نمی‌شه؛ بعد از ورود، خودت start.bat رو بزن.
echo   برای لغو: uninstall-service.bat
echo.
pause
exit /b 0
:noadmin
echo.
echo   Run as administrator: right-click this file ^> Run as administrator.
echo   راست‌کلیک روی فایل ^> Run as administrator
echo.
pause
exit /b 1
:nopy
echo   Python was not found. Install it first (tick "Add python.exe to PATH").
pause
exit /b 1
:fail
echo   FAILED to create the scheduled task.
pause
exit /b 1
