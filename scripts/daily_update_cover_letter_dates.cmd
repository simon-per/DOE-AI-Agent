@echo off
REM Daily wrapper for update_cover_letter_dates.py.
REM Runs at 05:45 local (Task: DOE_UpdateCoverLetterDates).
REM Re-stamps every cover letter folder with score >= 6 to TODAY's date,
REM so when the user applies right after waking up the letter date is current.
REM Logs to scripts\logs\update_dates_YYYYMMDD.log.
REM
REM Hardening (2026-04-28): keepawake, sentinel, fix parens %RC% bug.

setlocal enabledelayedexpansion

set "ROOT=%~dp0.."
cd /d "%ROOT%"

for /f "usebackq" %%d in (`powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"`) do set "LETTER_DATE=%%d"
for /f "usebackq" %%d in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"`) do set "STAMP=%%d"
if not exist "scripts\logs" mkdir "scripts\logs" >nul 2>&1
if not exist ".tmp" mkdir ".tmp" >nul 2>&1
set "LOG=scripts\logs\update_dates_!STAMP!.log"
set "PY=.venv\Scripts\python.exe"
set "SENTINEL_OK=.tmp\update_dates_last_success.txt"
set "SENTINEL_FAIL=.tmp\update_dates_last_failure.txt"
set "FRC=0"
set "KEEPAWAKE_PID="

if not exist "!PY!" (
    >>"!LOG!" echo ==== %DATE% %TIME% FAIL: python not found at !PY! ====
    set "FRC=9"
    goto cleanup
)

for /f "usebackq tokens=*" %%p in (`powershell -NoProfile -Command "(Start-Process powershell -ArgumentList '-NoProfile','-WindowStyle','Hidden','-File','%~dp0keepawake.ps1' -PassThru -WindowStyle Hidden).Id"`) do set "KEEPAWAKE_PID=%%p"

>>"!LOG!" echo ==== %DATE% %TIME% START (keepawake_pid=!KEEPAWAKE_PID!) ====
>>"!LOG!" echo Letter date (target): !LETTER_DATE!

call "%~dp0wait_for_network.cmd" >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
if not "!RC!"=="0" (
    >>"!LOG!" echo [%TIME%] FAIL wait_for_network rc=!RC!
    set "FRC=!RC!"
    goto fail
)

>>"!LOG!" echo [%TIME%] --- update_cover_letter_dates START ---
"!PY!" -m execution.update_cover_letter_dates --letter-date !LETTER_DATE! --min-score 7 >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
>>"!LOG!" echo [%TIME%] --- update_cover_letter_dates END rc=!RC! ---

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
