@echo off
REM Phase 14.B two-step launcher.
REM Usage: post_close.bat YYYY-MM-DD prepare
REM        post_close.bat YYYY-MM-DD apply
cd /d "%~dp0\.."
if "%~1"=="" (echo Usage: post_close.bat YYYY-MM-DD {prepare^|apply} & exit /b 2)
if "%~2"=="apply" (
  uv run python -m trading.jobs.post_close %1 --apply
) else (
  uv run python -m trading.jobs.post_close %1
)
