@echo off
rem Double-click to update Scout. It stops at once, changing nothing, while Scout is running.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m scout.running
if errorlevel 3 (
  echo.
  pause
  exit /b 1
)
git pull
if errorlevel 1 (
  echo.
  echo The update did not download. Copy the message above to Claude.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q -e ".[dev]"
if errorlevel 1 (
  echo.
  echo The update downloaded but did not install. Copy the message above to Claude.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m scout --version
echo.
echo Updated. Double-click "Start Scout" to start it again.
pause
