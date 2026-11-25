@echo off
rem Removes the Flow Agent service and binaries (keeps identity + outbox).
rem To also delete them run: uninstall.ps1 -PurgeData
setlocal
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
pause
