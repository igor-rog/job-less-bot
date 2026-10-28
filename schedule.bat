@echo off
rem Double-click once: runs the job pipeline every day at 8:00 AM while you're signed in to Windows.
rem If the computer was off or asleep at 8:00, it runs as soon as it can. Nothing is submitted or sent.
rem Another time: edit 8:00am below and double-click again. To remove it: unschedule.bat
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0schedule.ps1" -At 8:00am
pause
