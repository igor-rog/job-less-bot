@echo off
rem Removes the daily run that schedule.bat set up.
powershell -NoProfile -Command "Unregister-ScheduledTask -TaskName 'job-less-bot daily' -Confirm:$false; Write-Host 'Daily run removed.'"
pause
