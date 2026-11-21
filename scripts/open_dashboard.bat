@echo off
rem Opens the StealthBot control panel (the bot must be running: scripts\start.bat).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0open_dashboard.ps1" %*
if errorlevel 1 pause
