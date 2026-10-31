"""Taegliche Sicherung (24/7-Betrieb): SQLite-Datenbank (konsistent ueber die Backup-API, auch waehrend des Betriebs),
Forschungsurteile und Desk-Zustand. Aelteste Sicherungen werden nach `keep` Tagen geloescht."""
from __future__ import annotations

import shutil
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List


def backup_now(db_path: str, out_dir: str | Path | None = None, keep: int = 14,
               extra_files: tuple | None = None) -> Path:
    from src.firm.paths import data_dir
    out_dir = out_dir or data_dir() / "backups"
    extra_files = extra_files if extra_files is not None else (
        str(data_dir() / "strategy_verdicts.json"), os.getenv("FIRM_STATE_PATH", "firm_state.json"))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target = Path(out_dir) / stamp
    target.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(str(target / Path(db_path).name))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for f in extra_files:
        if Path(f).exists():
            shutil.copy2(f, target / Path(f).name)
    prune(out_dir, keep)
    return target


def prune(out_dir: str | Path, keep: int) -> List[str]:
    dirs = sorted(d for d in Path(out_dir).iterdir() if d.is_dir() and len(d.name) == 10)
    removed = []
    for d in dirs[:-keep] if keep > 0 else []:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed
