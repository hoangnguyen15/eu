@echo off
rem Double-click installer for the Flow Agent.
rem Put flowagent-config.json (downloaded from the admin console) next to this file.
setlocal
set "HERE=%~dp0"
if not exist "%HERE%flowagent-config.json" (
  echo [!] flowagent-config.json not found next to this file.
  echo     Create it in the admin console: Them may tram ^> Tao cau hinh cai dat.
  pause
  exit /b 1
)
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%install.ps1" -SourceDir "%HERE%." -ConfigFile "%HERE%flowagent-config.json"
if %errorlevel% neq 0 (
  echo.
  echo [!] Installation failed. See the messages above.
) else (
  echo.
  echo Installation finished.
)
pause
