@echo off
REM MG simulator LISTENS; the ASTAR middleware connects to it (hsms_mode: active).
setlocal
cd /d "%~dp0"

MGSimulator.exe --hsms-mode passive --port 5051 --wafers 3 --interval 0.5 --loop
set "SIM_EXIT=%ERRORLEVEL%"
if not "%SIM_EXIT%"=="0" (
  echo.
  echo MG Simulator stopped with exit code %SIM_EXIT%.
  pause
)
exit /b %SIM_EXIT%
