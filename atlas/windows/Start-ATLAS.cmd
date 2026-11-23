@echo off
rem Starts the ATLAS research stack: market scanner, shadow-trading test and the Hysilens dashboard.
rem It uses no API keys and places no orders. Close this window (or press Ctrl+C) to stop everything.
rem   Start-ATLAS.cmd           start and open the dashboard in your browser
rem   Start-ATLAS.cmd --login   start without opening the browser (used by the start-at-login shortcut)
title ATLAS research stack
cd /d "%~dp0.."
if not defined ATLAS_DATA set "ATLAS_DATA=%USERPROFILE%\.atlas\data"
set "OPEN=--open"
if /i "%~1"=="--login" set "OPEN="
set "PY=python"
where py >nul 2>nul && set "PY=py -3"

%PY% -m atlas.init_data --data "%ATLAS_DATA%"
if errorlevel 1 goto failed
%PY% -m atlas.supervisor --data "%ATLAS_DATA%" %OPEN%
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo ATLAS did not start. The message above says why.
pause
exit /b 1
