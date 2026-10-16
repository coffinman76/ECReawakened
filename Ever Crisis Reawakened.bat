@echo off
cd /d "%~dp0"
call "tools\setup-python.cmd"
if errorlevel 1 (
  echo Ever Crisis: Reawakened setup failed. Check your internet connection and try again.
  pause
  exit /b 1
)
start "" "python\pythonw.exe" "launcher.py"
