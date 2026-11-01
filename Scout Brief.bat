@echo off
rem Double-click to write the morning brief now and open it in Notepad. Safe while Scout runs.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m scout brief
if errorlevel 1 (
  echo.
  echo The brief was not written. Copy the message above to Claude.
  pause
  exit /b 1
)
set "LATEST="
for /f "delims=" %%F in ('dir /b /o-d "reports\scout-2*.md" 2^>nul') do if not defined LATEST set "LATEST=%%F"
if defined LATEST (
  start "" notepad "reports\%LATEST%"
) else (
  echo No brief found in the reports folder.
  pause
)
