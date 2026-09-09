@echo off
setlocal
title CryptO Research Lab V10
cd /d "%~dp0"
if not exist .venv (
    py -3 -m venv .venv
    if errorlevel 1 goto failed
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if errorlevel 1 goto failed
set OPEN_BROWSER=1
python app.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo Setup or startup failed. Read the error above. Python 3.10+ and internet access are required for the price feed.
pause
exit /b 1
