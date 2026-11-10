@echo off
rem Note your own read of a stock for the thesis-read paper test (READS_TEST.md 6). Scout asks four things:
rem the ticker, long or short, strength 1 to 5, and why (optional). Nothing trades on a read.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m scout reads add
echo.
if errorlevel 1 (
  echo The read was not noted. Read the message above, or copy it to Claude.
) else (
  echo Done.
)
pause
