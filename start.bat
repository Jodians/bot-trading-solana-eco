@echo off
REM start.bat - run the Solana Sniper (paper mode by default)
cd /d "%~dp0"
if not exist .venv (
  echo venv not found. Run setup.bat first.
  pause
  exit /b 1
)
if not exist .env (
  echo .env not found. Copying from example - EDIT IT before trading.
  copy .env.example .env
)
call .venv\Scripts\activate.bat
python snipe.py
pause
