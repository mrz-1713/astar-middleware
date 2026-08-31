@echo off
setlocal
cd /d "%~dp0"

SecsGemSimulator.exe check-config --config davinci-active.yaml
if errorlevel 1 (
  echo.
  echo Configuration is invalid. Fix davinci-active.yaml before retrying.
  pause
  exit /b 2
)

SecsGemSimulator.exe run --config davinci-active.yaml
set "SIM_EXIT=%ERRORLEVEL%"
if not "%SIM_EXIT%"=="0" (
  echo.
  echo DaVinci Simulator stopped with exit code %SIM_EXIT%.
  echo Review logs\secsgem-simulator.log for details.
  pause
)
exit /b %SIM_EXIT%
