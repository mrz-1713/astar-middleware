@echo off
REM The only thing anyone needs to double-click in this package.
REM
REM Opens the setup window, which asks which side of the installation this computer is
REM and then installs it. No PowerShell prompt, no execution-policy change, no
REM typed command, no config file to edit by hand.
setlocal
cd /d "%~dp0"

REM -ExecutionPolicy Bypass applies to this one process only; the machine's
REM own policy is left alone. Elevation happens inside Setup.ps1, at the
REM moment Install is pressed, so the UAC prompt arrives when it is expected
REM rather than before anything has been shown.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup.ps1"

if errorlevel 1 (
  echo.
  echo The setup window could not start.
  echo.
  echo Most likely cause: the ZIP was not extracted. Extract the whole folder
  echo first - Windows will run files from inside a ZIP viewer but they cannot
  echo see each other.
  echo.
  pause
)
