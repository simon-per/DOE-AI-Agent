@echo off
REM Manual fallback — DOE_PipelineFull Task Scheduler entry was retired 2026-05-06.
REM Modal `pipeline_full` (Tue/Thu 18:00 CEST cron) is the canonical run; this stays for ad-hoc local use.
REM Canonical order from directives/orchestration.md, plus prune+update bookends:
REM   prune -> scrape -> evaluate -> write -> cover -> cv -> update_dates
REM
REM Hardening (added 2026-04-28 after silent failure on 18:19 catch-up run):
REM   - Spawns keepawake.ps1 to block sleep while pipeline runs.
REM   - Per-stage timestamped START/END banners flushed line-by-line.
REM   - Writes .tmp/pipeline_last_success.txt or pipeline_last_failure.txt.
REM   - Fixed parser bug where %RC% inside parens IF block expanded empty.
REM   - delayedexpansion throughout so RC propagates correctly.

setlocal enabledelayedexpansion

set "ROOT=%~dp0.."
cd /d "%ROOT%"

for /f "usebackq" %%d in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"`) do set "STAMP=%%d"
for /f "usebackq" %%d in (`powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"`) do set "TODAY=%%d"
if not exist "scripts\logs" mkdir "scripts\logs" >nul 2>&1
if not exist ".tmp" mkdir ".tmp" >nul 2>&1
set "LOG=scripts\logs\pipeline_!STAMP!.log"
set "PY=.venv\Scripts\python.exe"
set "SENTINEL_OK=.tmp\pipeline_last_success.txt"
set "SENTINEL_FAIL=.tmp\pipeline_last_failure.txt"
set "STAGE_FILE=.tmp\pipeline_last_stage.txt"
set "FRC=0"
set "KEEPAWAKE_PID="

REM Verify python exists before doing anything else
if not exist "!PY!" (
    >>"!LOG!" echo ==== %DATE% %TIME% FAIL: python not found at !PY! ====
    set "FRC=9"
    goto cleanup
)

REM Spawn keepawake (hidden background powershell)
for /f "usebackq tokens=*" %%p in (`powershell -NoProfile -Command "(Start-Process powershell -ArgumentList '-NoProfile','-WindowStyle','Hidden','-File','%~dp0keepawake.ps1' -PassThru -WindowStyle Hidden).Id"`) do set "KEEPAWAKE_PID=%%p"

>>"!LOG!" echo ==== %DATE% %TIME% START (keepawake_pid=!KEEPAWAKE_PID!) ====

REM ----- network gate -----
>>"!LOG!" echo [%TIME%] step: wait_for_network
call "%~dp0wait_for_network.cmd" >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" (
    >>"!LOG!" echo [%TIME%] FAIL wait_for_network rc=!RC!
    set "FRC=!RC!"
    >"!STAGE_FILE!" echo wait_for_network
    goto fail
)
>>"!LOG!" echo [%TIME%] OK wait_for_network

REM ----- stage 0: prune_stale_jobs -----
>>"!LOG!" echo.
>>"!LOG!" echo [%TIME%] --- prune_stale_jobs START ---
>"!STAGE_FILE!" echo prune_stale_jobs
"!PY!" -m execution.prune_stale_jobs --yes >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
>>"!LOG!" echo [%TIME%] --- prune_stale_jobs END rc=!RC! ---
if not "!RC!"=="0" (
    set "FRC=!RC!"
    goto fail
)

REM ----- stage 1: scrape_jobs -----
>>"!LOG!" echo.
>>"!LOG!" echo [%TIME%] --- scrape_jobs START ---
>"!STAGE_FILE!" echo scrape_jobs
"!PY!" -m execution.scrape_jobs >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
>>"!LOG!" echo [%TIME%] --- scrape_jobs END rc=!RC! ---
if not "!RC!"=="0" (
    set "FRC=!RC!"
    goto fail
)

REM ----- stage 2: evaluate_jobs -----
>>"!LOG!" echo.
>>"!LOG!" echo [%TIME%] --- evaluate_jobs START ---
>"!STAGE_FILE!" echo evaluate_jobs
"!PY!" -m execution.evaluate_jobs >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
>>"!LOG!" echo [%TIME%] --- evaluate_jobs END rc=!RC! ---
if not "!RC!"=="0" (
    set "FRC=!RC!"
    goto fail
)

REM ----- stage 3: write_jobs_to_sheet -----
>>"!LOG!" echo.
>>"!LOG!" echo [%TIME%] --- write_jobs_to_sheet START ---
>"!STAGE_FILE!" echo write_jobs_to_sheet
"!PY!" -m execution.write_jobs_to_sheet >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
>>"!LOG!" echo [%TIME%] --- write_jobs_to_sheet END rc=!RC! ---
if not "!RC!"=="0" (
    set "FRC=!RC!"
    goto fail
)

REM ----- stage 4: generate_cover_letter -----
>>"!LOG!" echo.
>>"!LOG!" echo [%TIME%] --- generate_cover_letter START ---
>"!STAGE_FILE!" echo generate_cover_letter
"!PY!" -m execution.generate_cover_letter --min-score 6 >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
>>"!LOG!" echo [%TIME%] --- generate_cover_letter END rc=!RC! ---
if not "!RC!"=="0" (
    set "FRC=!RC!"
    goto fail
)

REM ----- stage 5: generate_cv -----
>>"!LOG!" echo.
>>"!LOG!" echo [%TIME%] --- generate_cv START ---
>"!STAGE_FILE!" echo generate_cv
"!PY!" -m execution.generate_cv --min-score 6 >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
>>"!LOG!" echo [%TIME%] --- generate_cv END rc=!RC! ---
if not "!RC!"=="0" (
    set "FRC=!RC!"
    goto fail
)

REM ----- stage 6: update_cover_letter_dates (re-stamp to today) -----
>>"!LOG!" echo.
>>"!LOG!" echo [%TIME%] --- update_cover_letter_dates START (date=!TODAY!) ---
>"!STAGE_FILE!" echo update_cover_letter_dates
"!PY!" -m execution.update_cover_letter_dates --letter-date !TODAY! --min-score 6 >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
>>"!LOG!" echo [%TIME%] --- update_cover_letter_dates END rc=!RC! ---
if not "!RC!"=="0" (
    set "FRC=!RC!"
    goto fail
)

REM ----- success -----
>>"!LOG!" echo.
>>"!LOG!" echo ==== %DATE% %TIME% SUCCESS ====
powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK'" > "!SENTINEL_OK!" 2>nul
if exist "!SENTINEL_FAIL!" del "!SENTINEL_FAIL!" >nul 2>&1
if exist "!STAGE_FILE!" del "!STAGE_FILE!" >nul 2>&1
set "FRC=0"
goto cleanup

:fail
>>"!LOG!" echo.
>>"!LOG!" echo ==== %DATE% %TIME% FAIL rc=!FRC! ====
powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK'" > "!SENTINEL_FAIL!" 2>nul
goto cleanup

:cleanup
if defined KEEPAWAKE_PID (
    taskkill /PID !KEEPAWAKE_PID! /F >nul 2>&1
    >>"!LOG!" echo [%TIME%] keepawake killed (pid=!KEEPAWAKE_PID!)
)
endlocal & exit /b %FRC%
