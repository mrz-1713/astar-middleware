@echo off
REM Opens the simulator control panel on simulator.yaml.
REM The panel is where the two settings that decide the wiring live:
REM   role -> EQUIPMENT or HOST   (which side this simulator pretends to be)
REM   mode -> PASSIVE or ACTIVE   (which side opens the TCP connection)
setlocal
cd /d "%~dp0"

start "" "AstarSimulatorGui.exe" --config "%~dp0simulator.yaml"
