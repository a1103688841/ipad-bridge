@echo off
rem Run with a console window (shows the log; Ctrl+C to quit).
cd /d "%~dp0"
.venv\Scripts\python.exe -m ipad_bridge %*
