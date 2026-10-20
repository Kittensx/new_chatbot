@echo off
title Unrestricted VLM Server Launcher
:: 1. Force the terminal to stay in the project's exact directory
cd /d "%~dp0"

echo ===================================================
echo   Activating your local Python Virtual Environment
echo ===================================================
:: 2. Call the activation script inside your "venv" folder
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
	:: Force Hugging Face to read strictly from your local disk cache
	set HF_HUB_OFFLINE=1
) else (
    echo [ERROR] Could not find a 'venv' folder in this directory!
    echo Please make sure this file is placed next to your venv folder.
    pause
    exit /b
)


echo ===================================================
echo   Launching Custom Unrestricted Vision Server
echo ===================================================
:: 3. Run the python backend script
python web_app.py

:: 4. Keep the command prompt window open if the program crashes or exits
pause
