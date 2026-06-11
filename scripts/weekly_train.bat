@echo off
REM Phase 18 weekly_train launcher (Sunday retrain + review).
REM Usage: weekly_train.bat [YYYY-MM-DD]
cd /d "%~dp0\.."
if "%~1"=="" (
  uv run trading weekly-train
) else (
  uv run trading weekly-train --date %1
)
