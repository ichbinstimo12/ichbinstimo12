@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  TRADING FIRM - PAPER MODE + DASHBOARD
echo  Dashboard: http://127.0.0.1:8000/
echo  Stop with CTRL+C
echo ============================================================
echo.

python -m src.firm run --dashboard

echo.
echo Firm stopped (exit code %ERRORLEVEL%).
pause
