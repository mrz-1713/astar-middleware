@echo off
REM Commissioning demo: refuse the GEM300 subscription band.
REM The middleware log must show gem300 REFUSED while the other bands stay
REM accepted, and per-lot CSV files must still be produced.
setlocal
cd /d "%~dp0"

MGSimulator.exe --hsms-mode passive --port 5051 --wafers 2 --refuse-band gem300 --loop
pause
