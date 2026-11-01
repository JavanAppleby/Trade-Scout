@echo off
rem Drop one of Jason's level files on this icon to add it. Double-click it to add the newest file in inbox\jason.
rem Scout checks the file, shows any warnings, and asks before adding. It never changes a file already added.
cd /d "%~dp0"
if not "%~1"=="" (
  ".venv\Scripts\python.exe" -m scout levels add "%~1"
) else (
  ".venv\Scripts\python.exe" -m scout levels add
)
echo.
if errorlevel 1 (
  echo The file was not added. Read the message above, or copy it to Claude.
) else (
  echo Done.
)
pause
