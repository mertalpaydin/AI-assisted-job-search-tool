@echo off
REM Activate UV virtual environment and run discovery script
cd /d %~dp0
.venv\Scripts\python.exe scripts\discover_fields.py %*
pause
