@echo off
REM Launches the control panel on the Python that install.ps1 installed.
REM ponytail: no PyInstaller build for the GUI - the interpreter and every
REM dependency are already on this box, so a shortcut is the whole packaging
REM step. Build a frozen exe only if the GUI must run where Python is absent.
setlocal
cd /d "%~dp0app"

REM pythonw = no console window. `start ""` lets this cmd window close at once.
start "" pythonw.exe -m gui.app --config "%~dp0app\config\production.yaml"
if errorlevel 1 (
  echo Could not start the control panel with pythonw.exe.
  echo Check that install.ps1 finished and that Python is on PATH.
  pause
)
