@echo off
echo ========================================
echo   KBTU AutoScraper - Installation
echo ========================================
echo.

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Install from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo [1/3] Creating virtual environment...
python -m venv venv
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create venv
    pause
    exit /b 1
)

echo [2/3] Installing dependencies...
call venv\Scripts\activate.bat
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)

echo [3/3] Installing Chromium browser...
playwright install chromium
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Chromium
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Installation complete!
echo ========================================
echo.
echo Next steps:
echo   1. Copy .env.example to .env and add your TELEGRAM_BOT_TOKEN
echo   2. Create users.json with logins, passwords and Telegram @usernames
echo   3. Run start.bat to launch
echo.
pause
