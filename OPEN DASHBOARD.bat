@echo off
REM Live browser dashboard for the form check (probe) and the blast.
REM Read-only: safe to open while anything runs. Close this window to stop it.
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0dashboard.py"
