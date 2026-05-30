@echo off
REM Daily apartment search: ingest -> score -> export -> notify (sends the action
REM brief to Simon's own inbox) -> Google Sheet sync. NO portal auto-send; the
REM brief is a one-tap review packet. A failing Sheet sync no longer aborts the
REM run (run_workflow swallows it), so the brief is always delivered.
REM
REM Wake-from-sleep is handled by the Task Scheduler task (WakeToRun=true,
REM StartWhenAvailable=true) -- see README "Automated daily run" -- not a
REM keepawake process. Logs to .tmp\logs\daily_YYYYMMDD.log.
setlocal enabledelayedexpansion

set "ROOT=%~dp0.."
cd /d "%ROOT%"

for /f "usebackq" %%d in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"`) do set "STAMP=%%d"
if not exist ".tmp\logs" mkdir ".tmp\logs" >nul 2>&1
set "LOG=.tmp\logs\daily_!STAMP!.log"

REM Force UTF-8 stdout so German listing text (umlauts, etc.) logs cleanly to the
REM redirected file instead of raising UnicodeEncodeError on the locale codepage.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

REM Prefer a project virtualenv, fall back to python on PATH.
set "PY=.venv\Scripts\python.exe"
if not exist "!PY!" set "PY=python"

>>"!LOG!" echo ==== %DATE% %TIME% START daily apartment run ====
"!PY!" execution\apartment_workflow.py --flatfox-max-pages 3 --notify-since 2d >>"!LOG!" 2>&1
set "RC=!ERRORLEVEL!"
>>"!LOG!" echo ==== %DATE% %TIME% END rc=!RC! ====

endlocal & exit /b %RC%
