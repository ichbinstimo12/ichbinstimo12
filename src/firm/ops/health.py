"""Gesundheit der Zonen fuer den 24/7-Betrieb (fuer Docker-Healthcheck und externe Ueberwachung).

money  frisch, wenn der letzte Konto-Snapshot juenger als `money_max_age` ist
brain  frisch, wenn der Agenten-Prozess sein Lebenszeichen (agent_registry) kuerzlich erneuert hat
halt   Halt-Flag aktiv (dann ist "nicht gesund" Absicht und muss von dir aufgehoben werden)
Gibt nur Ja/Nein und Alter zurueck, keine Kontozahlen.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def _age(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return time.time() - datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def zone_health(event_store, halt_file: Path, money_max_age: float = 300, brain_max_age: float = 900) -> Dict[str, Any]:
    snaps = event_store.get_events(types=["ACCOUNT_SNAPSHOT"], last=1)
    money_age = _age(snaps[-1]["ts"]) if snaps else None
    brain_age = None
    try:
        with sqlite3.connect(event_store.db_path) as conn:
            row = conn.execute("SELECT MAX(updated_at) FROM agent_registry").fetchone()
        brain_age = time.time() - row[0] if row and row[0] else None
    except sqlite3.Error:
        pass
    money_ok = money_age is not None and money_age <= money_max_age
    brain_ok = brain_age is not None and brain_age <= brain_max_age
    halted = halt_file.exists()
    return {"ok": money_ok and brain_ok and not halted, "halted": halted,
            "money": {"ok": money_ok, "age_s": None if money_age is None else round(money_age)},
            "brain": {"ok": brain_ok, "age_s": None if brain_age is None else round(brain_age)}}
