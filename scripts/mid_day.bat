@echo off
REM Phase 14.A two-step launcher.
REM Usage: mid_day.bat YYYY-MM-DD prepare
REM        mid_day.bat YYYY-MM-DD apply
cd /d "%~dp0\.."
if "%~1"=="" (echo Usage: mid_day.bat YYYY-MM-DD {prepare^|apply} & exit /b 2)
if "%~2"=="apply" (
  uv run python -m trading.jobs.mid_day %1 --apply
) else (
  uv run python -m trading.jobs.mid_day %1
)
