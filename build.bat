@echo off
rem Double-click to build VideoConspector.exe (no PowerShell policy problems).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe.ps1"
if errorlevel 1 (
    echo.
    echo BUILD FAILED. Send the text above to the developer.
)
pause
