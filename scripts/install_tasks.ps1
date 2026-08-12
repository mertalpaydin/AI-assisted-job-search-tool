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
$repo = Split-Path -Parent $PSScriptRoot
$bat  = Join-Path $repo "scripts\scheduled_run.bat"
$prefix = "JobSearch"

$tasks = @(
    @{ Name = "$prefix-Scrape";      Mode = "scrape";       Trigger = "Daily 06:30" },
    @{ Name = "$prefix-Screen";      Mode = "screen";       Trigger = "Daily 07:30" },
    @{ Name = "$prefix-CoverLetter"; Mode = "cover-letter"; Trigger = "Daily 08:00" },
    @{ Name = "$prefix-Collect";     Mode = "collect";      Trigger = "Hourly" },
    @{ Name = "$prefix-Clean";       Mode = "clean";        Trigger = "Weekly Sunday 03:00" }
)

foreach ($t in $tasks) {
    if (Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
        Write-Host "removed $($t.Name)"
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
    $parts = $spec -split " "
    switch ($parts[0]) {
        "Daily"  { return New-ScheduledTaskTrigger -Daily -At $parts[1] }
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
    }
}

foreach ($t in $tasks) {
    $action  = New-ScheduledTaskAction -Execute $bat -Argument $t.Mode -WorkingDirectory $repo
    $trigger = New-Trigger $t.Trigger
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Settings $settings -Description "AI job search: $($t.Mode)" | Out-Null
    Write-Host "registered $($t.Name)  ($($t.Trigger))"
}

Write-Host ""
Write-Host "Done. Before the first scheduled run, store a LinkedIn session:"
Write-Host "    uv run job-search login"
Write-Host "Check state any time with:"
Write-Host "    uv run job-search status"
