@echo off
REM Safety-net / fallback for the DOE pipeline.
REM Designed to run at user logon and every 1-2 hours via Task Scheduler.
REM
REM Reads sentinel files written by the daily/weekly tasks and fires a
REM recovery run if anything is missing or stale:
REM   - .tmp\prune_last_success.txt        -> daily, recover if >26h old
REM   - .tmp\update_dates_last_success.txt -> daily, recover if >26h old
REM   - .tmp\pipeline_last_success.txt     -> Tue+Thu, recover if >4d old
REM
REM Idempotent: uses .tmp\health_check.lock to prevent overlapping runs and
REM .tmp\recover_*_today.flag to avoid re-firing the same recovery on the
REM same calendar day.
REM
REM Logs to scripts\logs\health_check_YYYYMMDD.log.

setlocal enabledelayedexpansion

set "ROOT=%~dp0.."
cd /d "%ROOT%"

for /f "usebackq" %%d in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"`) do set "STAMP=%%d"
if not exist "scripts\logs" mkdir "scripts\logs" >nul 2>&1
if not exist ".tmp" mkdir ".tmp" >nul 2>&1
set "LOG=scripts\logs\health_check_!STAMP!.log"
set "LOCK=.tmp\health_check.lock"

REM ---- self-lock: skip if another health check is mid-flight ----
REM A pipeline recovery can take 30-60 min, so allow up to 6h before treating
REM the lock as stale (crashed prior run).
if exist "!LOCK!" (
    for /f "usebackq" %%h in (`powershell -NoProfile -Command "[int]((Get-Date) - (Get-Item '!LOCK!').LastWriteTime).TotalHours"`) do set "LOCK_AGE_H=%%h"
    if !LOCK_AGE_H! lss 6 (
        >>"!LOG!" echo [%DATE% %TIME%] lock held (age=!LOCK_AGE_H!h ^< 6h), exiting.
        endlocal & exit /b 0
    )
    >>"!LOG!" echo [%DATE% %TIME%] stale lock (age=!LOCK_AGE_H!h), reclaiming.
)
>"!LOCK!" echo %DATE% %TIME% pid=%RANDOM%

>>"!LOG!" echo ==== %DATE% %TIME% health_check START ====

REM ---- per-sentinel age check ----
REM Returns staleness (in hours) of each sentinel via PowerShell.
REM If sentinel does not exist, returns "MISSING".

call :check_sentinel "prune"        ".tmp\prune_last_success.txt"        26
set "PRUNE_NEEDS_RECOVERY=!NEEDS_RECOVERY!"
call :check_sentinel "update_dates" ".tmp\update_dates_last_success.txt" 26
set "DATES_NEEDS_RECOVERY=!NEEDS_RECOVERY!"
call :check_sentinel "pipeline"     ".tmp\pipeline_last_success.txt"     96
set "PIPELINE_NEEDS_RECOVERY=!NEEDS_RECOVERY!"

REM ---- skip-if-already-recovered-today guards ----
if "!PRUNE_NEEDS_RECOVERY!"=="1" (
    if exist ".tmp\recover_prune_!STAMP!.flag" (
        >>"!LOG!" echo [%TIME%] prune recovery already attempted today, skipping.
        set "PRUNE_NEEDS_RECOVERY=0"
    )
)
if "!DATES_NEEDS_RECOVERY!"=="1" (
    if exist ".tmp\recover_dates_!STAMP!.flag" (
        >>"!LOG!" echo [%TIME%] update_dates recovery already attempted today, skipping.
        set "DATES_NEEDS_RECOVERY=0"
    )
)
if "!PIPELINE_NEEDS_RECOVERY!"=="1" (
    if exist ".tmp\recover_pipeline_!STAMP!.flag" (
        >>"!LOG!" echo [%TIME%] pipeline recovery already attempted today, skipping.
        set "PIPELINE_NEEDS_RECOVERY=0"
    )
)

REM ---- network gate (cheap) before firing any recovery ----
set "NET_OK=0"
if "!PRUNE_NEEDS_RECOVERY!!DATES_NEEDS_RECOVERY!!PIPELINE_NEEDS_RECOVERY!" NEQ "000" (
    >>"!LOG!" echo [%TIME%] some recovery needed; probing network...
    powershell -NoProfile -Command "try { $c = New-Object System.Net.Sockets.TcpClient; $r = $c.BeginConnect('oauth2.googleapis.com', 443, $null, $null); if ($r.AsyncWaitHandle.WaitOne(5000, $false)) { $c.EndConnect($r); $c.Close(); exit 0 } else { $c.Close(); exit 1 } } catch { exit 1 }"
    if !ERRORLEVEL!==0 (
        set "NET_OK=1"
        >>"!LOG!" echo [%TIME%] network OK.
    ) else (
        >>"!LOG!" echo [%TIME%] network unreachable; will retry on next health_check tick.
    )
)

REM ---- fire recoveries (longest first to not race the daily window) ----
if "!NET_OK!"=="1" (
    if "!PIPELINE_NEEDS_RECOVERY!"=="1" (
        >>"!LOG!" echo [%TIME%] firing pipeline_full recovery...
        >".tmp\recover_pipeline_!STAMP!.flag" echo %DATE% %TIME%
        call "%~dp0pipeline_full.cmd" >>"!LOG!" 2>&1
        set "RC=!ERRORLEVEL!"
        >>"!LOG!" echo [%TIME%] pipeline_full recovery rc=!RC!
        REM pipeline_full re-stamps cover letter dates as its final stage,
        REM so a successful pipeline run also satisfies the dates sentinel.
        if "!RC!"=="0" set "DATES_NEEDS_RECOVERY=0"
        REM and it runs prune as stage 0, so prune is also satisfied.
        if "!RC!"=="0" set "PRUNE_NEEDS_RECOVERY=0"
    )

    if "!PRUNE_NEEDS_RECOVERY!"=="1" (
        >>"!LOG!" echo [%TIME%] firing prune_stale_jobs recovery...
        >".tmp\recover_prune_!STAMP!.flag" echo %DATE% %TIME%
        call "%~dp0daily_prune_stale_jobs.cmd" >>"!LOG!" 2>&1
        set "RC=!ERRORLEVEL!"
        >>"!LOG!" echo [%TIME%] prune recovery rc=!RC!
    )

    if "!DATES_NEEDS_RECOVERY!"=="1" (
        >>"!LOG!" echo [%TIME%] firing update_cover_letter_dates recovery...
        >".tmp\recover_dates_!STAMP!.flag" echo %DATE% %TIME%
        call "%~dp0daily_update_cover_letter_dates.cmd" >>"!LOG!" 2>&1
        set "RC=!ERRORLEVEL!"
        >>"!LOG!" echo [%TIME%] update_dates recovery rc=!RC!
    )
)

>>"!LOG!" echo ==== %DATE% %TIME% health_check END ====
del "!LOCK!" >nul 2>&1
endlocal & exit /b 0


REM ===========================================================================
REM :check_sentinel <label> <path> <max_age_hours>
REM Sets NEEDS_RECOVERY=1 if file is missing or older than max_age_hours.
REM ===========================================================================
:check_sentinel
set "LBL=%~1"
set "FILE=%~2"
set "MAX_HOURS=%~3"
set "NEEDS_RECOVERY=0"
if not exist "!FILE!" (
    >>"!LOG!" echo [%TIME%] sentinel !LBL! MISSING (file: !FILE!)
    set "NEEDS_RECOVERY=1"
    goto :eof
)
for /f "usebackq" %%h in (`powershell -NoProfile -Command "[int]((Get-Date) - (Get-Item '!FILE!').LastWriteTime).TotalHours"`) do set "AGE_H=%%h"
>>"!LOG!" echo [%TIME%] sentinel !LBL! age=!AGE_H!h (threshold=!MAX_HOURS!h)
if !AGE_H! geq !MAX_HOURS! set "NEEDS_RECOVERY=1"
goto :eof
