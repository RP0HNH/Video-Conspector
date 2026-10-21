@echo off
rem Double-click to run Video Conspector from source (no exe needed).
rem First start creates .venv and installs packages (a few minutes).
cd /d "%~dp0"

if exist ".venv\pyvenv.cfg" (
    findstr /i "conda" ".venv\pyvenv.cfg" >nul && (
        echo .venv is based on Anaconda - recreating it
        rmdir /s /q .venv
    )
)

if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv || py -3.11 -m venv .venv || py -3.10 -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo Python 3.10-3.12 from python.org was not found.
        echo Install it:  winget install Python.Python.3.12
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo pip install failed.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" gui.py
if errorlevel 1 pause
