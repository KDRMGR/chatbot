@echo off
rem STEALTHBOT - quick health probe of the running API.
curl.exe -s --max-time 5 http://127.0.0.1:8000/api/health
if errorlevel 1 (
  echo.
  echo STEALTHBOT API is not reachable on 127.0.0.1:8000
  exit /b 1
)
echo.
