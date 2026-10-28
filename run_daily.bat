@echo off
rem The daily pipeline, as Task Scheduler runs it (see schedule.bat). Output goes to data\logs\daily.log.
cd /d "%~dp0"
if not exist data\logs mkdir data\logs
echo. >> data\logs\daily.log
echo ===== %date% %time% ===== >> data\logs\daily.log
.venv\Scripts\python.exe -X utf8 -m pipeline.run --scheduled >> data\logs\daily.log 2>&1
