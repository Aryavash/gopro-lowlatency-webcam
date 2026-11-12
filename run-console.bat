@echo off
rem Runs the webcam in this window with live logs (for troubleshooting).
rem Quit the tray copy first - only one copy can run at a time.
title GoPro low-latency webcam (console)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Not installed yet. Run:  powershell -ExecutionPolicy Bypass -File install.ps1
    pause
    exit /b 1
)
".venv\Scripts\python.exe" gopro_cam.py %*
pause
