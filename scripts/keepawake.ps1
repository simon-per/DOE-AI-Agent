# Prevents Windows from entering sleep / hibernate while this process runs.
# Pipeline cmds spawn this hidden, store the PID, and taskkill it at end.
# Works without admin. The flag is per-thread, so the script must stay alive.
#
# Why: 2026-04-28 18:19 pipeline run was killed mid-flight by a sleep cycle
# at 18:30:28 (kernel-power event 42). Same pattern killed 2026-04-27 prune.

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class KeepAwake {
    [DllImport("kernel32.dll")]
    public static extern uint SetThreadExecutionState(uint flags);
}
"@

# ES_CONTINUOUS (0x80000000) | ES_SYSTEM_REQUIRED (0x00000001)
# = keep the SYSTEM awake (display can sleep, that's fine).
[void][KeepAwake]::SetThreadExecutionState(0x80000001)

while ($true) { Start-Sleep -Seconds 60 }
