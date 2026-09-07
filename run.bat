@echo off
cd /d "%~dp0"
if exist .venv\Scripts\pythonw.exe (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.py"
) else (
    start "" pythonw.exe "%~dp0app.py"
)
if errorlevel 1 pause
