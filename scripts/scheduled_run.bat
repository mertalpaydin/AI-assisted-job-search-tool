@echo off
REM Wrapper for Windows Task Scheduler.
REM
REM   scheduled_run.bat scrape     search + details                  (needs LinkedIn)
REM   scheduled_run.bat screen-cl  screening (batch) + cover letters (no LinkedIn)
REM   scheduled_run.bat collect    collect finished screening batches (no LinkedIn)
REM   scheduled_run.bat clean      expiry sweep                      (needs LinkedIn)
REM
REM screen-cl runs both stages in ONE process so they never collide on the
REM single run lock. Screening batches on a scheduled run; cover letters stay
REM instant (grounded) and process the approved backlog without waiting.
REM
REM --scheduled implies --no-interactive and honours the schedule pause, so a
REM run can never open a browser on a machine nobody is sitting at.

setlocal
cd /d "%~dp0.."

set MODE=%~1
if "%MODE%"=="" set MODE=scrape

if /i "%MODE%"=="scrape" (
    uv run job-search run --resume --scheduled --max-runtime 1 -s search -s details
) else if /i "%MODE%"=="screen-cl" (
    uv run job-search run --resume --scheduled --max-runtime 1 -s screen -s cover-letter
) else if /i "%MODE%"=="collect" (
    uv run job-search batch collect
) else if /i "%MODE%"=="clean" (
    uv run job-search clean --scheduled
) else (
    echo Unknown mode "%MODE%". Use scrape^|screen-cl^|collect^|clean.
    exit /b 1
)

exit /b %ERRORLEVEL%
