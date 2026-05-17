@echo off
REM Daily wrapper for hydrate_volume_from_drive.py.
REM Runs at 05:45 local (Task: DOE_HydrateApplications) — replaces DOE_UpdateCoverLetterDates.
REM Pulls latest application folders from Drive (canonical) into local .tmp/applications/
REM so the apply swarm (Stage 7) sees fresh, accurately-dated cover letters.
REM Modal pipeline_daily_maintenance at 03:00 CEST owns the re-stamping; this task just mirrors.
REM Logs to scripts\logs\hydrate_YYYYMMDD.log.

setlocal enabledelayedexpansion

set "ROOT=%~dp0.."
cd /d "%ROOT%"

for /f "usebackq" %%d in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"`) do set "STAMP=%%d"
if not exist "scripts\logs" mkdir "scripts\logs" >nul 2>&1
if not exist ".tmp" mkdir ".tmp" >nul 2>&1
set "LOG=scripts\logs\hydrate_!STAMP!.log"
set "PY=.venv\Scripts\python.exe"
set "SENTINEL_OK=.tmp\hydrate_last_success.txt"
set "SENTINEL_FAIL=.tmp\hydrate_last_failure.txt"
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

>>"!LOG!" echo [%TIME%] --- hydrate_volume_from_drive START ---
"!PY!" -m execution.hydrate_volume_from_drive --since-days 365 >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
>>"!LOG!" echo [%TIME%] --- hydrate_volume_from_drive END rc=!RC! ---

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
