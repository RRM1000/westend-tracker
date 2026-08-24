@echo off
REM ---------------------------------------------------------------------------
REM  West End Tracker - daily collection.
REM
REM  Collects today's prices for every show in config\shows.yaml, then rebuilds
REM  the visual report. Roughly 7 minutes for 10 shows.
REM
REM  Run it by hand any time by double-clicking this file, or leave it to the
REM  scheduled task. Output is appended to data\logs\collect.log.
REM ---------------------------------------------------------------------------

cd /d "%~dp0"
if not exist "data\logs" mkdir "data\logs"

set LOG=data\logs\collect.log

echo. >> "%LOG%"
echo ================================================== >> "%LOG%"
echo RUN STARTED %DATE% %TIME% >> "%LOG%"
echo ================================================== >> "%LOG%"

".venv\Scripts\python.exe" -m wet.cli calendars >> "%LOG%" 2>&1
set COLLECT_RC=%ERRORLEVEL%

".venv\Scripts\python.exe" -m wet.cli export >> "%LOG%" 2>&1

echo RUN FINISHED %DATE% %TIME% (collect exit code %COLLECT_RC%) >> "%LOG%"

exit /b %COLLECT_RC%
