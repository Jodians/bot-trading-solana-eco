@echo off
REM kill-dashboard.bat — bunuh semua proses yang pegang port 8765/8766
setlocal enabledelayedexpansion
for %%p in (8765 8766) do (
  for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%%p .*LISTEN"') do (
    echo kill PID %%a (port %%p)
    taskkill /PID %%a /F 2>nul
  )
)
timeout /t 1 /nobreak >nul
echo --- remaining listeners ---
netstat -ano 2>nul | findstr ":8765 .*LISTEN" | findstr /v "TIME_WAIT"
netstat -ano 2>nul | findstr ":8766 .*LISTEN" | findstr /v "TIME_WAIT"
echo --- done ---
