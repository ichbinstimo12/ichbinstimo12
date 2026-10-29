@echo off
rem Jeder Befehl der Firma mit der richtigen Python-Umgebung (.venv), z. B.:
rem   firm status   firm doctor   firm eval   firm halt   firm resume   firm notify-test   firm approve-live
rem Beim ersten Aufruf wird .venv angelegt und alles aus requirements.txt installiert.
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
)
if not exist ".venv\Scripts\python.exe" (
    if not defined PY (
        echo [FEHLER] Python 3.11 oder neuer wurde nicht gefunden.
        echo          Installieren: https://www.python.org/downloads/  ^(Haken bei "Add python.exe to PATH" setzen^)
        exit /b 1
    )
    echo Lege Python-Umgebung .venv an ...
    %PY% -m venv .venv || (echo [FEHLER] venv konnte nicht angelegt werden. & exit /b 1)
)
set "VPY=.venv\Scripts\python.exe"

fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul
if errorlevel 1 (
    echo Installiere/aktualisiere Pakete aus requirements.txt ^(einmalig, dauert ein paar Minuten^) ...
    "%VPY%" -m pip install --upgrade pip >nul
    "%VPY%" -m pip install -r requirements.txt || (echo [FEHLER] Paketinstallation fehlgeschlagen - siehe Meldungen oben. & exit /b 1)
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

if "%~1"=="" (
    "%VPY%" -m src.firm --help
    exit /b 0
)
"%VPY%" -m src.firm %*
exit /b %ERRORLEVEL%
