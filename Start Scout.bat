@echo off
rem Double-click to start Scout. It gathers market context and never trades. Stop it with Ctrl+C in this window.
cd /d "%~dp0"
title Scout
".venv\Scripts\python.exe" -m scout run
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="4" (
  echo Scout is already running in another window. Nothing was started here and that Scout keeps running.
  pause
  exit /b 4
)
if not "%RC%"=="0" echo Scout did not start or stopped with an error. Copy the message above to Claude.
echo Scout has stopped. Double-click "Start Scout" to start it again.
pause
