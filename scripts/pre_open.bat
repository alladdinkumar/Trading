@echo off
REM Phase 13 MVP launcher. Phase 17 will wire this into Windows Task Scheduler.
REM Usage: pre_open.bat YYYY-MM-DD
cd /d "%~dp0\.."
if "%~1"=="" (
  echo Usage: pre_open.bat YYYY-MM-DD
  exit /b 2
)
uv run python -m trading.jobs.pre_open %1
