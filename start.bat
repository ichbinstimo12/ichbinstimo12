@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  TRADING FIRM - PAPER MODE + DASHBOARD
echo  Dashboard: http://127.0.0.1:8000/
echo  Stop with CTRL+C
echo ============================================================
echo.

rem --- Python 3.11+ finden (py-Launcher bevorzugt) ---------------------------
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [FEHLER] Python 3.11 oder neuer wurde nicht gefunden.
    echo          Installieren: https://www.python.org/downloads/  ^(Haken bei "Add python.exe to PATH" setzen^)
    goto :end
)

rem --- Eigene Umgebung im Projektordner (.venv), einmalig anlegen -----------
if not exist ".venv\Scripts\python.exe" (
    echo Lege Python-Umgebung .venv an ...
    %PY% -m venv .venv || (echo [FEHLER] venv konnte nicht angelegt werden. & goto :end)
)
set "VPY=.venv\Scripts\python.exe"

rem --- Pakete installieren, wenn requirements.txt neu ist oder sich geaendert hat
fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul
if errorlevel 1 (
    echo Installiere/aktualisiere Pakete aus requirements.txt ^(einmalig, dauert ein paar Minuten^) ...
    "%VPY%" -m pip install --upgrade pip >nul
    "%VPY%" -m pip install -r requirements.txt || (echo [FEHLER] Paketinstallation fehlgeschlagen - siehe Meldungen oben. & goto :end)
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

if not exist ".env" (
    echo [HINWEIS] Keine .env gefunden. Kopiere deine bisherige .env in diesen Ordner
    echo           oder lege sie an: copy .env.example .env  ^(dann Schluessel eintragen^)
    echo.
)

"%VPY%" -m src.firm run --dashboard

:end
echo.
echo Firm stopped (exit code %ERRORLEVEL%).
pause
