@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  TRADING FIRM - PAPER MODE + DASHBOARD
echo  Dashboard: http://127.0.0.1:8000/
echo  Stop with CTRL+C
echo  Andere Befehle: firm status / firm eval / firm halt / firm resume
echo ============================================================
echo.

if not exist ".env" (
    echo [HINWEIS] Keine .env gefunden. Kopiere deine bisherige .env in diesen Ordner
    echo           oder lege sie an: copy .env.example .env  ^(dann Schluessel eintragen^)
    echo.
)

call "%~dp0firm.bat" run --dashboard

echo.
echo Firm stopped (exit code %ERRORLEVEL%).
pause
