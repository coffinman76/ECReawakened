@echo off
setlocal
cd /d "%~dp0.."
if exist "python\python.exe" goto dependencies
if not exist ".python-setup" mkdir ".python-setup"
echo Downloading Python for Ever Crisis: Reawakened...
curl.exe --fail --location --retry 2 "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip" --output ".python-setup\python.zip"
if errorlevel 1 exit /b 1
rem -m skips restoring file times, which fails in some folders (cloud-synced, antivirus-scanned).
tar.exe -xmf ".python-setup\python.zip" -C ".python-setup"
if not exist ".python-setup\python.exe" exit /b 1
> ".python-setup\python312._pth" echo python312.zip
>> ".python-setup\python312._pth" echo .
>> ".python-setup\python312._pth" echo ..
>> ".python-setup\python312._pth" echo Lib\site-packages
>> ".python-setup\python312._pth" echo import site
del ".python-setup\python.zip"
move ".python-setup" "python" >nul
if errorlevel 1 exit /b 1
:dependencies
"python\python.exe" "tools\setup_runtime.py"
exit /b %errorlevel%
