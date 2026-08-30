@echo off
REM ════════════════════════════════════════════════════════════════
REM  bot-start.bat — Jalankan Solana Sniper (log ke file, minified)
REM  - Bot jalan di background, log realtime ke solana-bot.log
REM  - Pantau: buka solana-bot.log di editor, atau powershell:
REM        Get-Content solana-bot.log -Wait
REM  - Stop: tutup window ini, atau taskkill /IM python.exe
REM
REM  PYTHONPATH di-clear (biar gak kecemar venv Hermes)
REM ════════════════════════════════════════════════════════════════
setlocal
set "BOT=C:\Users\ASUS\bot-trading-solana-eco"
set "PYTHONPATH="
cd /d "%BOT%"
call .\.venv\Scripts\Activate.ps1 >nul 2>&1
echo [%date% %time%] Bot starting... > "%BOT%\solana-bot.log"
python snipe.py >> "%BOT%\solana-bot.log" 2>&1
endlocal
