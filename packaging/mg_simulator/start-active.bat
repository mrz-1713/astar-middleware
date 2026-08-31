@echo off
REM MG simulator DIALS OUT; the ASTAR middleware listens (hsms_mode: passive).
REM Replace 192.168.1.20 with the middleware server IP.
setlocal
cd /d "%~dp0"

MGSimulator.exe --hsms-mode active --host 192.168.1.20 --port 5051 --wafers 3 --loop
set "SIM_EXIT=%ERRORLEVEL%"
if not "%SIM_EXIT%"=="0" (
  echo.
  echo MG Simulator stopped with exit code %SIM_EXIT%.
  pause
)
exit /b %SIM_EXIT%
