# Register the scheduled tasks in Windows Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Remove
#
# Every task uses StartWhenAvailable, so a run missed while the laptop was
# asleep fires as soon as it wakes instead of being skipped until tomorrow.
# None of them wake the machine; add -WakeToRun below if you want that.

param([switch]$Remove)

$ErrorActionPreference = "Stop"
# Ensure script runs with Administrator privileges so triggers (Logon, Event) and operational logs can be configured
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    try {
        $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
        if ($Remove) { $argList += "-Remove" }
        $proc = Start-Process powershell.exe -ArgumentList $argList -Verb RunAs -Wait -PassThru -ErrorAction Stop
        exit $proc.ExitCode
    } catch {
        # Self-elevation via UI prompt is not supported in non-interactive/background console runners.
        Write-Host "Running with current user privileges."
    }
}

$repo = Split-Path -Parent $PSScriptRoot
$bat  = Join-Path $repo "scripts\scheduled_run.bat"
$prefix = "JobSearch"

# "daily" runs scraping and then screening+cover-letters back to back in one
# task, so the second leg starts when the first actually finishes rather than
# after a guessed 15-minute gap that scraping twice overran.
#
# Catchup runs the SAME mode upon waking from sleep or hibernation (WakeResume)
# or at logon (Catchup). On a laptop the 07:00 trigger is usually missed because
# the machine is asleep or hibernating, and StartWhenAvailable did not reliably
# recover it. All daily triggers are guarded by --once-daily, so whichever gets
# there first does the work and the others exit immediately.
$tasks = @(
    @{ Name = "$prefix-Daily";      Mode = "daily";    Trigger = "Daily 07:00" },
    @{ Name = "$prefix-WakeResume"; Mode = "daily";    Trigger = "AtResume" },
    @{ Name = "$prefix-Catchup";    Mode = "daily";    Trigger = "AtLogOn" },
    @{ Name = "$prefix-Collect";    Mode = "collect";   Trigger = "Daily 08:00, 20:00" },
    @{ Name = "$prefix-Clean";      Mode = "clean";     Trigger = "Weekly Sunday 03:00" },
    @{ Name = "$prefix-External";   Mode = "external";  Trigger = "Weekly Sunday 08:00" }
)

# Remove orphaned JobSearch-* tasks, but leave currently active ones in place
Get-ScheduledTask -TaskName "$prefix-*" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($Remove -or ($tasks.Name -notcontains $_.TaskName)) {
        Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
        Write-Host "removed $($_.TaskName)"
    }
}
if ($Remove) { Write-Host "All JobSearch tasks removed."; exit 0 }

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)

function New-Trigger($spec) {
    if ($spec -is [array]) {
        return $spec | ForEach-Object { New-Trigger $_ }
    }
    $parts = $spec -split "\s+"
    switch ($parts[0]) {
        "Daily" {
            $times = ($parts[1..($parts.Length - 1)] -join " ") -split "[,\s]+" | Where-Object { $_ }
            return $times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
        }
        "Weekly" { return New-ScheduledTaskTrigger -Weekly -DaysOfWeek $parts[1] -At $parts[2] }
        "Hourly" {
            # Batch results land within 24h; hourly polling is cheap and keeps
            # collection independent of when you happen to open the app.
            $t = New-ScheduledTaskTrigger -Once -At (Get-Date)
            $t.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
                -RepetitionInterval (New-TimeSpan -Hours 1) `
                -RepetitionDuration (New-TimeSpan -Days 3650)).Repetition
            return $t
        }
        "AtLogOn" {
            # Five minutes after logon, so it is not competing with everything
            # else Windows starts. --once-daily makes it a no-op on any day the
            # 07:00 trigger already did the work.
            $t = New-ScheduledTaskTrigger -AtLogOn
            $t.Delay = "PT5M"
            return $t
        }
        "AtResume" {
            # Two minutes after waking from sleep or hibernation (S4 / Modern Standby),
            # allowing network (Wi-Fi) to reconnect.
            # Event ID 1: Microsoft-Windows-Power-Troubleshooter (returned from low power state / hibernation)
            # Event ID 107/507: Microsoft-Windows-Kernel-Power (resumed from sleep / exiting Modern Standby)
            $class = Get-CimClass -Namespace "Root/Microsoft/Windows/TaskScheduler" -ClassName MSFT_TaskEventTrigger
            $query = "<QueryList><Query Id='0' Path='System'><Select Path='System'>*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]</Select><Select Path='System'>*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=107 or EventID=507)]]</Select></Query></QueryList>"
            return New-CimInstance -CimClass $class -ClientOnly -Property @{
                Enabled      = $true
                Subscription = $query
                Delay        = "PT2M"
            }
        }
    }
}

$user = $env:USERNAME

foreach ($t in $tasks) {
    $action  = New-ScheduledTaskAction -Execute $bat -Argument $t.Mode -WorkingDirectory $repo
    $trigger = New-Trigger $t.Trigger
    try {
        Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
            -Settings $settings -User $user -Description "AI job search: $($t.Mode)" -Force | Out-Null
        Write-Host "registered $($t.Name)  ($($t.Trigger))"
    } catch {
        Write-Warning "Could not register $($t.Name): $($_.Exception.Message)"
    }
}

# Windows keeps no record of why a task did not fire unless this log is on, and
# it is off by default. That absence is why a scheduled run silently going
# missing could not be explained after the fact. Needs admin; harmless if it
# fails, since nothing here depends on it.
$opLog = "Microsoft-Windows-TaskScheduler/Operational"
try {
    $cfg = New-Object System.Diagnostics.Eventing.Reader.EventLogConfiguration $opLog
    if (-not $cfg.IsEnabled) {
        $cfg.IsEnabled = $true
        $cfg.SaveChanges()
        Write-Host "enabled $opLog (task history)"
    }
} catch {
    Write-Host "note: could not enable $opLog - run as administrator to get task history"
}

Write-Host ""
Write-Host "Done. Before the first scheduled run, store a LinkedIn session:"
Write-Host "    uv run job-search login"
Write-Host "Check state any time with:"
Write-Host "    uv run job-search status"
Write-Host ""
Write-Host "The daily work now runs from two triggers: 07:00 and 5 minutes after"
Write-Host "logon. Whichever fires first does the work; the other exits."
