"""Risiko-Overlay: der einzige Weg, auf dem Agenten den Handel beeinflussen koennen.

Ein Agent (Risk Officer, Macro Analyst) darf fuer einen Markt oder fuer alle ("*") das Risiko pro Trade
REDUZIEREN (Faktor 0..1) oder neue Einstiege ganz sperren (Faktor 0), immer befristet. Erhoehen ist im Code
unmoeglich: Faktoren > 1 werden auf 1 gekappt, und der Desk nimmt das Minimum aller aktiven Eintraege.
Bestehende Positionen und Stops werden nie angefasst. Jeder Eintrag ist im Event-Log nachvollziehbar.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

MAX_HOURS = 48.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_overlays (
    id TEXT PRIMARY KEY,
    epic TEXT NOT NULL,
    factor REAL NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    agent_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT
);
"""


class RiskOverlay:
    def __init__(self, db_path: str, event_store: Any = None, now_fn=time.time):
        self.db_path, self.event_store, self.now = db_path, event_store, now_fn
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def tighten(self, epic: str, factor: float, hours: float, agent_id: str, reason: str,
                source: Optional[str] = None) -> Dict[str, Any]:
        """Risiko fuer `epic` (oder "*") befristet auf `factor` (0..1) senken. Gibt den gespeicherten Eintrag zurueck."""
        epic = (epic or "").strip().upper() or "*"
        factor = max(0.0, min(1.0, float(factor)))
        hours = max(0.25, min(MAX_HOURS, float(hours)))
        reason = " ".join(str(reason or "").split())[:300]
        if not reason:
            raise ValueError("an overlay needs a reason")
        now = self.now()
        row = {"id": str(uuid.uuid4()), "epic": epic, "factor": factor, "created_at": now,
               "expires_at": now + hours * 3600, "agent_id": agent_id, "reason": reason, "source": source}
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO risk_overlays VALUES (:id, :epic, :factor, :created_at, :expires_at, "
                         ":agent_id, :reason, :source)", row)
        if self.event_store is not None:
            self.event_store.append(agent_id, "RISK_TIGHTENED", dict(row, hours=hours))
        return row

    def active(self) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM risk_overlays WHERE expires_at > ? ORDER BY created_at",
                                (self.now(),)).fetchall()
        return [dict(r) for r in rows]

    def factor(self, epic: str) -> tuple[float, Optional[str]]:
        """(Faktor, Begruendung) fuer einen Markt. 1.0 = keine Einschraenkung."""
        best, why = 1.0, None
        for r in self.active():
            if r["epic"] in ("*", epic.upper()) and r["factor"] < best:
                best, why = r["factor"], f"{r['agent_id']}: {r['reason']}"
        return best, why
