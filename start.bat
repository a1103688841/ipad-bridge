@echo off
rem Run in the background (tray icon only, no console window).
cd /d "%~dp0"
start "" .venv\Scripts\pythonw.exe -m ipad_bridge %*
