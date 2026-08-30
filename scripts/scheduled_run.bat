@echo off
REM Wrapper for Windows Task Scheduler.
REM
REM   scheduled_run.bat daily      scrape, then screening + cover letters
REM   scheduled_run.bat collect    collect finished screening batches (no LinkedIn)
REM   scheduled_run.bat clean      expiry sweep                      (needs LinkedIn)
REM
REM   scheduled_run.bat scrape     just the scrape leg               (manual use)
REM   scheduled_run.bat screen-cl  just the screening leg            (manual use)
REM
REM "daily" runs the two legs back to back in one task. They used to be two
REM tasks 15 minutes apart, which assumed scraping always finished inside its
REM hour: twice it did not, and screening exited with "another run is already
REM in progress" instead. Running them in sequence means the second leg starts
REM when the first actually ends, however long that takes.
REM
REM Each leg carries --once-daily, so the same "daily" mode is safe to fire
REM from both the 07:00 trigger and the logon catch-up: whichever runs first
REM does the work, the other sees it is done and exits. If scraping succeeded
REM but screening died, the catch-up re-runs only the screening.
REM
REM The screening leg scrapes details BEFORE it screens, which is the point of
REM the "-s details" in front. Search fills the details queue faster than five
REM detail workers drain it and never stops refilling, so the scrape leg's hour
REM always expires with a backlog. Screening then ran over whatever happened to
REM be finished, and the rest waited a day while the next morning piled more on
REM top. This leg runs no search worker, so its queue is finite and drains, and
REM the batch it submits covers the whole day's intake rather than a fraction.
REM
REM The clean leg carries no --max-runtime: the bound lives in config.yaml as
REM cleaner.scheduled_max_runtime_hours, so it applies to scheduled sweeps only
REM and an on-demand "job-search clean" runs to completion.
REM
REM --scheduled implies --no-interactive and honours the schedule pause, so a
REM run can never open a browser on a machine nobody is sitting at.

setlocal
cd /d "%~dp0.."

set MODE=%~1
if "%MODE%"=="" set MODE=daily

if /i "%MODE%"=="daily" (
    uv run job-search run --resume --scheduled --once-daily scrape --max-runtime 1 -s search -s details
    uv run job-search run --resume --scheduled --once-daily screen-cl --max-runtime 2 -s details -s screen -s cover-letter
) else if /i "%MODE%"=="scrape" (
    uv run job-search run --resume --scheduled --max-runtime 1 -s search -s details
) else if /i "%MODE%"=="screen-cl" (
    uv run job-search run --resume --scheduled --max-runtime 2 -s details -s screen -s cover-letter
) else if /i "%MODE%"=="collect" (
    uv run job-search batch collect
) else if /i "%MODE%"=="clean" (
    uv run job-search clean --scheduled
) else (
    echo Unknown mode "%MODE%". Use daily^|collect^|clean^|scrape^|screen-cl.
    exit /b 1
)

exit /b %ERRORLEVEL%
