"""Welche Agenten gerade arbeiten (DB-Tabelle), damit das Dashboard das auch im getrennten Prozessbetrieb weiss."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from typing import Any, Dict, Iterable, List

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_registry (
    name TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def write_agents(db_path: str, configs: Iterable[Any]) -> None:
    rows = [(c.name, json.dumps({k: v for k, v in asdict(c).items() if k != "system_prompt"}), time.time())
            for c in configs if c is not None]
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        conn.execute("DELETE FROM agent_registry")
        conn.executemany("INSERT INTO agent_registry VALUES (?, ?, ?)", rows)


def read_agents(db_path: str) -> List[Dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        rows = conn.execute("SELECT config_json, updated_at FROM agent_registry ORDER BY name").fetchall()
    return [dict(json.loads(c), registry_updated_at=u) for c, u in rows]
