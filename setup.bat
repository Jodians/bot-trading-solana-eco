@echo off
REM setup.bat - one-time: create venv + install deps for Solana Sniper
cd /d "%~dp0"
echo [1/3] Creating virtualenv...
python -m venv .venv
if errorlevel 1 (
  echo python -m venv failed. Make sure Python 3.11+ is on PATH.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
echo [2/3] Installing dependencies...
pip install -r requirements.txt 2>nul || uv pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency install failed.
  pause
  exit /b 1
)
echo [3/3] Copying .env.example -> .env (edit it before running)
if not exist .env copy .env.example .env
echo.
echo Setup done. Edit .env, then run start.bat
pause
