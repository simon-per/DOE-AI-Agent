# Registering the safety-net (DOE_HealthCheck)

This document holds the one-time commands to wire `check_pipeline_health.cmd`
into Task Scheduler. **Nothing in this file runs automatically** — copy each
PowerShell block into an **elevated PowerShell window** when you're ready.

The existing three tasks (`DOE_PipelineFull`, `DOE_PruneStaleJobs`,
`DOE_UpdateCoverLetterDates`) are **not modified** by these commands.

---

## 1. Register `DOE_HealthCheck`

Triggers: at logon + every 90 min while logged in.
Behaviour: reads sentinels in `.tmp\` and recovers any task that's stale or missed.

```powershell
$Action  = New-ScheduledTaskAction -Execute "C:\Users\simon\Desktop\DOE AI Agent\scripts\check_pipeline_health.cmd"
$Trigger1 = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Trigger2 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(8) -RepetitionInterval (New-TimeSpan -Minutes 90) -RepetitionDuration (New-TimeSpan -Hours 23)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 4) -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "DOE_HealthCheck" `
    -Action $Action -Trigger $Trigger1, $Trigger2 `
    -Settings $Settings -Principal $Principal `
    -Description "Reads .tmp/*_last_success.txt sentinels and re-fires any DOE task that missed its window."
```

Verify:

```powershell
Get-ScheduledTask -TaskName "DOE_HealthCheck" | Get-ScheduledTaskInfo
```

---

## 2. (Optional) Add `WakeToRun` to the daily/weekly tasks

Today's silent failure was a sleep event killing the cmd mid-flight; that's
already fixed by `keepawake.ps1`. But the trigger itself is still missed
when the machine is asleep at 05:45 / 17:00 — `StartWhenAvailable=True`
catches it up later, and `DOE_HealthCheck` covers any further misses.

If you'd rather have the machine wake itself on schedule:

```powershell
foreach ($t in 'DOE_PipelineFull','DOE_PruneStaleJobs','DOE_UpdateCoverLetterDates') {
    $task = Get-ScheduledTask -TaskName $t
    $task.Settings.WakeToRun = $true
    Set-ScheduledTask -TaskName $t -Settings $task.Settings
}
```

Skip this section if you prefer the laptop only runs when you're using it.
The health-check covers either way.

---

## 3. Manual smoke test (do this once before trusting it)

Run from any cmd or PowerShell prompt with the project root as cwd:

```cmd
scripts\check_pipeline_health.cmd
```

Then inspect:

- `scripts\logs\health_check_<YYYYMMDD>.log` — should show sentinel ages
  and either "no recovery needed" or "firing X recovery"
- `.tmp\health_check.lock` — should be **gone** after exit (only present
  during a run)

If a recovery fires and you want to abort it, kill the spawned cmd window;
the safety net's `recover_*_<date>.flag` will prevent it from re-firing
on the same day.

---

## 4. Sentinel reference

| File | Written by | Used by | Stale threshold |
|------|-----------|---------|-----------------|
| `.tmp\pipeline_last_success.txt`     | `pipeline_full.cmd`               | `check_pipeline_health.cmd` | 96h (4 days) |
| `.tmp\pipeline_last_failure.txt`     | `pipeline_full.cmd` (on fail)     | manual inspection            | n/a |
| `.tmp\pipeline_last_stage.txt`       | `pipeline_full.cmd` (on fail)     | manual inspection            | n/a |
| `.tmp\prune_last_success.txt`        | `daily_prune_stale_jobs.cmd`      | `check_pipeline_health.cmd` | 26h |
| `.tmp\prune_last_failure.txt`        | `daily_prune_stale_jobs.cmd`      | manual inspection            | n/a |
| `.tmp\update_dates_last_success.txt` | `daily_update_cover_letter_dates.cmd` | `check_pipeline_health.cmd` | 26h |
| `.tmp\update_dates_last_failure.txt` | `daily_update_cover_letter_dates.cmd` | manual inspection           | n/a |
| `.tmp\health_check.lock`             | `check_pipeline_health.cmd` (live) | self (mutex)                 | 6h |
| `.tmp\recover_*_<YYYYMMDD>.flag`     | `check_pipeline_health.cmd` (on recovery fire) | self (idempotency) | calendar day |

---

## 5. Rollback

Remove just the safety net:

```powershell
Unregister-ScheduledTask -TaskName "DOE_HealthCheck" -Confirm:$false
```

The original three tasks are untouched.
