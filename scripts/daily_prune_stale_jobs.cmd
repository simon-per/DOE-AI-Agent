@echo off
REM Daily wrapper for prune_stale_jobs.py.
REM Manual fallback — DOE_PruneStaleJobs Task Scheduler entry was retired 2026-05-06.
REM Modal `pipeline_daily_maintenance` (06:00 CEST cron) handles this in the cloud now.
REM Deletes rows where Status in (New, Irrelevant, Rejected) AND newest of
REM Date Scraped/Date Posted is older than 30 days, OR Score <= 4.
REM Logs to scripts\logs\prune_stale_jobs_YYYYMMDD.log.
REM
REM Hardening (2026-04-28): keepawake, sentinel, fix parens %RC% bug.

setlocal enabledelayedexpansion

set "ROOT=%~dp0.."
cd /d "%ROOT%"

for /f "usebackq" %%d in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"`) do set "STAMP=%%d"
if not exist "scripts\logs" mkdir "scripts\logs" >nul 2>&1
if not exist ".tmp" mkdir ".tmp" >nul 2>&1
set "LOG=scripts\logs\prune_stale_jobs_!STAMP!.log"
set "PY=.venv\Scripts\python.exe"
set "SENTINEL_OK=.tmp\prune_last_success.txt"
set "SENTINEL_FAIL=.tmp\prune_last_failure.txt"
set "FRC=0"
set "KEEPAWAKE_PID="

if not exist "!PY!" (
    >>"!LOG!" echo ==== %DATE% %TIME% FAIL: python not found at !PY! ====
    set "FRC=9"
    goto cleanup
)

for /f "usebackq tokens=*" %%p in (`powershell -NoProfile -Command "(Start-Process powershell -ArgumentList '-NoProfile','-WindowStyle','Hidden','-File','%~dp0keepawake.ps1' -PassThru -WindowStyle Hidden).Id"`) do set "KEEPAWAKE_PID=%%p"

>>"!LOG!" echo ==== %DATE% %TIME% START (keepawake_pid=!KEEPAWAKE_PID!) ====

call "%~dp0wait_for_network.cmd" >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" (
    >>"!LOG!" echo [%TIME%] FAIL wait_for_network rc=!RC!
    set "FRC=!RC!"
    goto fail
)

>>"!LOG!" echo [%TIME%] --- prune_stale_jobs START ---
"!PY!" -m execution.prune_stale_jobs --yes >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
>>"!LOG!" echo [%TIME%] --- prune_stale_jobs END rc=!RC! ---

if not "!RC!"=="0" (
    set "FRC=!RC!"
    goto fail
)

>>"!LOG!" echo ==== %DATE% %TIME% SUCCESS ====
powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK'" > "!SENTINEL_OK!" 2>nul
if exist "!SENTINEL_FAIL!" del "!SENTINEL_FAIL!" >nul 2>&1
set "FRC=0"
goto cleanup

:fail
>>"!LOG!" echo ==== %DATE% %TIME% FAIL rc=!FRC! ====
powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK'" > "!SENTINEL_FAIL!" 2>nul

:cleanup
if defined KEEPAWAKE_PID (
    taskkill /PID !KEEPAWAKE_PID! /F >nul 2>&1
)
endlocal & exit /b %FRC%
