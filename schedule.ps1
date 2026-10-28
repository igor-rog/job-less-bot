# Called by schedule.bat. Registers the daily run for the signed-in user (no password stored).
param([string]$At = "8:00am")
$bat = Join-Path $PSScriptRoot "run_daily.bat"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c start `"`" /min `"$bat`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# StartWhenAvailable: if the PC was off or asleep at that time, run as soon as it's back.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName "job-less-bot daily" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Scheduled: job-less-bot runs every day at $At. Log: data\logs\daily.log"
