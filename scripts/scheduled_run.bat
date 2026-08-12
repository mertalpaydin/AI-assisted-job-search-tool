@echo off
REM Wrapper for Windows Task Scheduler.
REM
REM   scheduled_run.bat scrape        search + details        (needs LinkedIn)
REM   scheduled_run.bat screen        screening only          (no LinkedIn)
REM   scheduled_run.bat cover-letter  cover letters only      (no LinkedIn)
REM   scheduled_run.bat collect       collect batch results   (no LinkedIn)
REM   scheduled_run.bat clean         expiry sweep            (needs LinkedIn)
REM
REM --scheduled implies --no-interactive and honours the schedule pause, so a
REM run can never open a browser on a machine nobody is sitting at.

setlocal
cd /d "%~dp0.."

set MODE=%~1
if "%MODE%"=="" set MODE=scrape

if /i "%MODE%"=="scrape" (
    uv run job-search run --resume --scheduled --max-runtime 1.5 -s search -s details
) else if /i "%MODE%"=="screen" (
    uv run job-search run --resume --scheduled --max-runtime 1.5 -s screen
) else if /i "%MODE%"=="cover-letter" (
    uv run job-search run --resume --scheduled --max-runtime 1 -s cover-letter
) else if /i "%MODE%"=="collect" (
    uv run job-search batch collect
) else if /i "%MODE%"=="clean" (
    uv run job-search clean --scheduled
) else (
    echo Unknown mode "%MODE%". Use scrape^|screen^|cover-letter^|collect^|clean.
    exit /b 1
)

exit /b %ERRORLEVEL%
