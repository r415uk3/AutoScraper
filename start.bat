@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat
python open_kbtu.py
if errorlevel 1 pause
