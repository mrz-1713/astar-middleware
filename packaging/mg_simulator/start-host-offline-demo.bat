@echo off
REM Commissioning demo: tool starts in HOST OFF-LINE.
REM Without request_online: true in production.yaml the middleware connects
REM green and receives NOTHING. With it, S1F17 lifts the tool and events flow.
setlocal
cd /d "%~dp0"

MGSimulator.exe --hsms-mode passive --port 5051 --wafers 2 --start-offline --loop
pause
