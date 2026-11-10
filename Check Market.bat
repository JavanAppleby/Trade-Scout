@echo off
rem Check the market feed of the thesis reads: whether Alpaca answered, the newest prices of SPY, QQQ and
rem IWM with their age (in the early hours they are old, and that is fine), what a reader would see now,
rem and whether the key in Scout's .env works. It never shows the key and writes no record or state, only
rem its log lines. Nothing trades on it.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m scout reads market
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo Done: Alpaca answered and the key works. Old prices in the early hours are normal.
) else if "%RC%"=="2" (
  echo The market feed is not set up yet. Read the message above, or copy it to Claude.
) else (
  echo The check could not confirm the key: Alpaca gave no answer Scout could read. Read the message above, or copy it to Claude.
)
pause
