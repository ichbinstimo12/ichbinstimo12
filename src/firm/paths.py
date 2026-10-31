"""Wo das Gedaechtnis der Firma liegt - getrennt vom Programmcode, damit es Updates ueberlebt.

Frueher lagen Datenbank (Lektionen, Playbooks, Ereignisse), Zustand, Archiv, Berichte und Forschungsurteile relativ im
Programmordner. Wer eine neue Version in einen neuen Ordner entpackte, startete mit leerem Gedaechtnis.

Jetzt (Standard):  Windows  %USERPROFILE%\\TradingFirm-Daten     Linux/macOS  ~/.trading-firm
                   Docker   /app/data (FIRM_HOME im Dockerfile)
Eigener Ort:       FIRM_HOME=D:\\Firma\\Daten   ·   altes Verhalten (alles im Programmordner): FIRM_HOME=project

Absolute Pfade (FIRM_DB_PATH=D:\\..., ...) haben Vorrang; relative gelten ab dem Datenordner.
Beim ersten Start mit leerem Datenordner wird das Gedaechtnis aus dem Programmordner KOPIERT (Original bleibt).
`firm memory import <alter Ordner>` holt es aus einem frueheren Entpack-Ordner (vorher Sicherung des aktuellen Stands).
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("Paths")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_NAME = "firm_events.db"
# Datei/Ordner im Programmordner -> Umgebungsvariable, relativ zum Datenordner
LAYOUT = {
    "FIRM_DB_PATH": DB_NAME,
    "FIRM_STATE_PATH": "firm_state.json",
    "FIRM_HALT_FILE": "firm.halt",
    "FIRM_LIVE_TOKEN_FILE": "live_token.json",
    "FIRM_ARCHIVE_DIR": "archiv",
    "FIRM_REPORT_DIR": "reports",
    "FIRM_DATA_DIR": "data",
    "FIRM_BACKUP_DIR": "data/backups",
    "FIRM_STUDIO_DIR": "data/studio/products",
    "NOTIFY_CURSOR_FILE": "data/notify_cursor.txt",
}
# Was beim Uebernehmen kopiert wird (kein Halt-Flag, kein Live-Token: beides gilt nur fuer den alten Lauf)
COPY_FILES = ("firm_state.json",)
COPY_DIRS = ("archiv", "reports", "data")
SKIP_IN_DATA = {"backups", "mcp"}       # Sicherungen sind gross, MCP-Arbeitsordner ist fluechtig


def default_home() -> Optional[Path]:
    raw = os.getenv("FIRM_HOME", "").strip()
    if raw.lower() == "project":
        return None
    if raw:
        return Path(raw).expanduser()
    if sys.platform.startswith("win"):
        return Path(os.environ.get("USERPROFILE") or Path.home()) / "TradingFirm-Daten"
    return Path.home() / ".trading-firm"


def data_dir() -> Path:
    """Ordner fuer Forschungsurteile, Kursdaten, Studio-Pakete ... (relativ 'data', wenn kein Datenordner gilt)."""
    return Path(os.getenv("FIRM_DATA_DIR", "data"))


def _copy_db(src: Path, dst: Path) -> None:
    """Konsistente Kopie auch einer laufenden/WAL-Datenbank (SQLite-Backup-API statt Dateikopie)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(src)) as s, sqlite3.connect(str(dst)) as d:
        s.backup(d)


def _has_memory(db: Path) -> bool:
    if not db.exists():
        return False
    try:
        with sqlite3.connect(str(db)) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            n = 0
            for t in ("events", "lessons"):
                if t in tables:
                    n += c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            return n > 0
    except sqlite3.Error:
        return False


def _copy_tree(src_root: Path, home: Path) -> None:
    for name in COPY_FILES:
        if (src_root / name).is_file():
            home.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_root / name, home / name)
    for name in COPY_DIRS:
        src = src_root / name
        if not src.is_dir():
            continue
        ignore = shutil.ignore_patterns(*SKIP_IN_DATA) if name == "data" else None
        shutil.copytree(src, home / name, dirs_exist_ok=True, ignore=ignore)


def lesson_count(db: Path) -> int:
    try:
        with sqlite3.connect(str(db)) as c:
            return int(c.execute("SELECT COUNT(*) FROM lessons").fetchone()[0])
    except sqlite3.Error:
        return 0


def apply_home(project_root: Path = PROJECT_ROOT) -> Optional[Path]:
    """Datenordner bestimmen, Pfade setzen (ohne explizite zu ueberschreiben), beim ersten Mal uebernehmen."""
    try:
        from src.firm.envload import load_env
        load_env()                       # FIRM_HOME darf in .env stehen (respektiert FIRM_NO_DOTENV)
    except Exception:
        pass
    home = default_home()
    if home is None:
        return None
    home.mkdir(parents=True, exist_ok=True)
    for key, rel in LAYOUT.items():
        v = os.environ.get(key, "").strip()
        # leer -> Standard im Datenordner; relativ (z. B. FIRM_DB_PATH=firm_events.db aus einer alten .env) ->
        # ebenfalls im Datenordner, sonst laege das Gedaechtnis wieder im Programmordner; absolut -> bleibt
        os.environ[key] = str(home / (v or rel)) if not v or not Path(v).is_absolute() else v
    db = Path(os.environ["FIRM_DB_PATH"])
    legacy_db = Path(project_root) / DB_NAME
    if not _has_memory(db) and _has_memory(legacy_db) and legacy_db.resolve() != db.resolve():
        _copy_db(legacy_db, db)
        _copy_tree(Path(project_root), home)
        logger.info(f"Gedaechtnis aus dem Programmordner uebernommen ({lesson_count(db)} Lektionen) -> {home}")
    return home


def import_memory(old_root: Path, home: Path) -> Dict[str, object]:
    """Gedaechtnis aus einem frueheren Programm-/Entpack-Ordner holen. Der aktuelle Stand wird vorher gesichert."""
    old_root, home = Path(old_root), Path(home)
    old_db = old_root / DB_NAME
    if not _has_memory(old_db):
        raise FileNotFoundError(f"In {old_root} liegt keine {DB_NAME} mit Gedaechtnis")
    home.mkdir(parents=True, exist_ok=True)
    db = Path(os.getenv("FIRM_DB_PATH") or home / DB_NAME)
    backup = None
    if db.exists():
        backup = home / f"vor_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        _copy_db(db, backup)
    _copy_db(old_db, db)
    _copy_tree(old_root, home)
    return {"from": str(old_root), "to": str(home), "lessons": lesson_count(db),
            "backup": str(backup) if backup else None}
