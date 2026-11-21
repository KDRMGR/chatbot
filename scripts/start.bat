@echo off
rem STEALTHBOT - start the bot and its local API + dashboard (loopback only).
rem Close this window or press Ctrl+C to stop. The dashboard's Stop and Restart buttons work with this launcher.
setlocal
set "VENV=%LOCALAPPDATA%\stealthbot\venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo Virtual environment not found at %VENV%
  echo Run:  powershell -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
  exit /b 1
)
cd /d "%~dp0..\backend"
"%VENV%\Scripts\python.exe" -m app.run
