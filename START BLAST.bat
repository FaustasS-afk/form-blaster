@echo off
REM Start the blaster AND the live dashboard together.
REM The dashboard is read-only, so it is safe to leave open.
REM
REM FIXED 2026-11-03: this file referenced "venv", but the actual virtualenv is
REM ".venv". The launch silently failed and the run died after a few lines.

cd /d "%~dp0"

set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" set PY=%~dp0venv\Scripts\python.exe
if not exist "%PY%" (
  echo Could not find .venv\Scripts\python.exe - create the venv first.
  pause
  exit /b 1
)

echo Starting live dashboard...
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0live_watch.py"
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0dashboard.py"

echo Starting blast (resuming pending leads)...
"%PY%" -u "%~dp0blast_full.py"

echo.
echo Blast finished. Dashboard still open - press Ctrl+C there to close.
pause
