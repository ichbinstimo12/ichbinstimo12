"""Produktkatalog des Studios: Zustand je Produkt (SQLite) + jede Aenderung als Ereignis (append-only).

Zustaende:
  IDEA -> DRAFTED -> AWAITING_APPROVAL -> APPROVED -> PUBLISHED -> RETIRED
                  \\-> AUDIT_FAILED (Pruefung nicht bestanden; ein Ueberarbeitungsversuch, dann Schluss)
  AWAITING_APPROVAL -> REJECTED (du lehnst ab)

Nur der Operator (du, ueber Dashboard/CLI mit Token) darf APPROVED, REJECTED, PUBLISHED und RETIRED setzen.
Das Studio selbst kommt hoechstens bis AWAITING_APPROVAL. Damit geht nichts ohne dich an Kunden.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

OPERATOR_ONLY = {"APPROVED", "REJECTED", "PUBLISHED", "RETIRED"}
TRANSITIONS = {
    "IDEA": {"DRAFTED", "AUDIT_FAILED"},
    "DRAFTED": {"AWAITING_APPROVAL", "AUDIT_FAILED"},
    "AUDIT_FAILED": {"DRAFTED"},
    "AWAITING_APPROVAL": {"APPROVED", "REJECTED"},
    "APPROVED": {"PUBLISHED", "REJECTED"},
    "PUBLISHED": {"RETIRED"},
    "REJECTED": set(),
    "RETIRED": set(),
}
OPEN_STATES = ("IDEA", "DRAFTED", "AWAITING_APPROVAL", "APPROVED")


class TransitionError(ValueError):
    pass


class Catalog:
    def __init__(self, db_path: str, event_store=None):
        self.db_path, self.event_store = db_path, event_store
        with sqlite3.connect(self.db_path) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS studio_products (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL,
                title TEXT NOT NULL, niche TEXT, format TEXT, language TEXT, price_usd REAL,
                revisions INTEGER NOT NULL DEFAULT 0, platform_ref TEXT, data_json TEXT NOT NULL DEFAULT '{}')""")

    # ------------------------------------------------------------------ read
    @staticmethod
    def _row(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        d["data"] = json.loads(d.pop("data_json") or "{}")
        return d

    def get(self, pid: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as c:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM studio_products WHERE id = ?", (pid,)).fetchone()
        return self._row(r) if r else None

    def list(self, statuses: Optional[List[str]] = None, limit: int = 200) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM studio_products", []
        if statuses:
            q += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args = list(statuses)
        q += " ORDER BY created_at DESC LIMIT ?"
        with sqlite3.connect(self.db_path) as c:
            c.row_factory = sqlite3.Row
            return [self._row(r) for r in c.execute(q, (*args, int(limit))).fetchall()]

    def count(self, statuses: List[str]) -> int:
        return len(self.list(statuses, limit=10_000))

    def created_since(self, iso_ts: str) -> int:
        with sqlite3.connect(self.db_path) as c:
            return int(c.execute("SELECT COUNT(*) FROM studio_products WHERE created_at >= ?", (iso_ts,)).fetchone()[0])

    def by_platform_ref(self, ref: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as c:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM studio_products WHERE platform_ref = ?", (ref,)).fetchone()
        return self._row(r) if r else None

    # ------------------------------------------------------------------ write
    def create(self, title: str, niche: str, fmt: str, language: str, price_usd: float,
               data: Dict[str, Any], actor: str) -> Dict[str, Any]:
        now = datetime.utcnow().isoformat()
        pid = "p_" + uuid.uuid4().hex[:10]
        with sqlite3.connect(self.db_path) as c:
            c.execute("INSERT INTO studio_products (id, created_at, updated_at, status, title, niche, format, language,"
                      " price_usd, data_json) VALUES (?, ?, ?, 'IDEA', ?, ?, ?, ?, ?, ?)",
                      (pid, now, now, title, niche, fmt, language, float(price_usd), json.dumps(data)))
        self._log(actor, "STUDIO_IDEA", {"product_id": pid, "title": title, "niche": niche, "format": fmt,
                                         "price_usd": price_usd, "why": data.get("why")})
        return self.get(pid)

    def transition(self, pid: str, to: str, actor: str, operator: bool = False, note: str = "",
                   **fields: Any) -> Dict[str, Any]:
        p = self.get(pid)
        if p is None:
            raise TransitionError(f"Produkt {pid} unbekannt")
        if to in OPERATOR_ONLY and not operator:
            raise TransitionError(f"{to} darf nur der Operator setzen")
        if to not in TRANSITIONS[p["status"]]:
            raise TransitionError(f"{p['status']} -> {to} ist nicht erlaubt")
        data = dict(p["data"])
        data.update(fields.pop("data", None) or {})
        cols = {"status": to, "updated_at": datetime.utcnow().isoformat(), "data_json": json.dumps(data)}
        for k in ("title", "price_usd", "platform_ref"):
            if k in fields and fields[k] is not None:
                cols[k] = fields[k]
        if to == "DRAFTED" and p["status"] == "AUDIT_FAILED":
            cols["revisions"] = int(p["revisions"]) + 1
        with sqlite3.connect(self.db_path) as c:
            c.execute(f"UPDATE studio_products SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                      (*cols.values(), pid))
        self._log(actor, f"STUDIO_{to}", {"product_id": pid, "title": cols.get("title", p["title"]),
                                          "from": p["status"], "note": note[:500],
                                          **({"platform_ref": cols["platform_ref"]} if "platform_ref" in cols else {})})
        return self.get(pid)

    def _log(self, actor: str, kind: str, payload: Dict[str, Any]) -> None:
        if self.event_store is not None:
            self.event_store.append(actor, kind, payload, correlation_id=payload.get("product_id"))
