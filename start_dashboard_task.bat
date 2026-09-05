@echo off
REM ============================================================
REM  start_dashboard_task.bat - wrapper for Task Scheduler.
REM
REM  Why this exists: Task Scheduler's /tr argument mangles paths that contain
REM  spaces ("Solana Dashboard.bat" sits on the Desktop). This wrapper lives at a
REM  space-free path so schtasks can call it verbatim; it just forwards to the
REM  real launcher, which keeps ALL the singleton / restart / rotation logic in
REM  one place.
REM
REM  Registered as:  schtasks /tn SolanaDashboard
REM  Remove with:    schtasks /delete /tn SolanaDashboard /f
REM ============================================================
call "C:\Users\ASUS\Desktop\Solana Dashboard.bat"
