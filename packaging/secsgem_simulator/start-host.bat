@echo off
REM Runs the simulator as the HOST (the EAP side) against a real tool.
REM Edit host-example.yaml first: connection.address must be the EQUIPMENT IP.
REM The middleware is not part of this link.
setlocal
cd /d "%~dp0"

SecsGemSimulator.exe check-config --config host-example.yaml
if errorlevel 1 (
  echo.
  echo Configuration is invalid. Fix host-example.yaml before retrying.
  pause
  exit /b 2
)

SecsGemSimulator.exe run --config host-example.yaml
set "SIM_EXIT=%ERRORLEVEL%"
if not "%SIM_EXIT%"=="0" (
  echo.
  echo Host simulator stopped with exit code %SIM_EXIT%.
  echo Review logs\secsgem-simulator.log for details.
  pause
)
exit /b %SIM_EXIT%
