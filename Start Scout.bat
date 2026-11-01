@echo off
rem Double-click to start Scout. It gathers market context and never trades. Stop it with Ctrl+C in this window.
cd /d "%~dp0"
title Scout
".venv\Scripts\python.exe" -m scout run
echo.
echo Scout has stopped. Double-click "Start Scout" to start it again.
pause
