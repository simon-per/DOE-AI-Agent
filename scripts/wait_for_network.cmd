@echo off
REM Block until oauth2.googleapis.com:443 is reachable.
REM 20 attempts x 5 min = up to ~100 min total wait.
REM Returns 0 on success, 1 if still unreachable after the budget.
REM Stdout is meant to be appended to the caller's log file.

setlocal enabledelayedexpansion
set /a TRIES=0
echo [%DATE% %TIME%] wait_for_network: probing oauth2.googleapis.com:443

:loop
powershell -NoProfile -Command "try { $c = New-Object System.Net.Sockets.TcpClient; $r = $c.BeginConnect('oauth2.googleapis.com', 443, $null, $null); if ($r.AsyncWaitHandle.WaitOne(5000, $false)) { $c.EndConnect($r); $c.Close(); exit 0 } else { $c.Close(); exit 1 } } catch { exit 1 }"
if !ERRORLEVEL!==0 (
    echo [%DATE% %TIME%] wait_for_network: OK after !TRIES! retries.
    endlocal & exit /b 0
)

set /a TRIES+=1
if !TRIES! geq 20 (
    echo [%DATE% %TIME%] wait_for_network: still unreachable after 20 tries, giving up.
    endlocal & exit /b 1
)

echo [%DATE% %TIME%] wait_for_network: try !TRIES!/20 failed, sleeping 5 min...
timeout /t 300 /nobreak >nul
goto loop
