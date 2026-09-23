# Register or unregister the external job search scheduled task in Windows Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_external_search.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_external_search.ps1 -Remove
#
# Runs every Sunday at 08:00 AM using `job-search external-search --scheduled`.

param([switch]$Remove)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$bat  = Join-Path $repo "scripts\scheduled_run.bat"
$taskName = "JobSearch-External"

# Check if already present and remove if requested or updating
Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | ForEach-Object {
    Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
    Write-Host "Removed existing task: $($_.TaskName)"
}

if ($Remove) {
    Write-Host "External job search scheduled task removed."
    exit 0
}

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)

$action  = New-ScheduledTaskAction -Execute $bat -Argument "external" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 08:00

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "AI external job search: weekly Sunday run" -Force | Out-Null

Write-Host "Successfully registered scheduled task: $taskName (Every Sunday at 08:00 AM)"
