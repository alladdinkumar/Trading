@echo off
REM Phase 14.C pre-open IEP gap filter launcher.
REM Usage: pre_open_iep.bat YYYY-MM-DD
REM Run at 08:55 IST, after /kite-quotes-snapshot has captured fresh quotes.
cd /d "%~dp0\.."
if "%~1"=="" (echo Usage: pre_open_iep.bat YYYY-MM-DD & exit /b 2)
uv run trading pre-open-iep --date %1
