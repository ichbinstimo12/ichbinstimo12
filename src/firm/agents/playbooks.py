"""Prozedurales Gedaechtnis: versionierte Arbeitsanleitungen (Playbooks) pro Rolle.

- Startversion v1 kommt aus config/playbooks/<Rolle>.md.
- Ein Agent kann eine Aenderung nur VORSCHLAGEN (Regel hinzufuegen oder streichen). Der Vorschlag wird mit Diff
  gespeichert und ist erst nach Freigabe durch den Operator (Dashboard/API) aktiv.
- Jede Version bleibt erhalten (nichts wird ueberschrieben), jede Entscheidung ist ein Event.
- Playbooks sind Text fuer den Prompt. Sie koennen nie Limits, Modus oder Freigaben aendern.
"""
from __future__ import annotations

import difflib
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_PLAYBOOK_CHARS = 4000
MAX_RULE_CHARS = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playbooks (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    version INTEGER,
    text TEXT NOT NULL,
    status TEXT NOT NULL,          -- ACTIVE | PROPOSED | ARCHIVED | REJECTED | SUPERSEDED
    proposed_by TEXT,
    reason TEXT,
    diff TEXT,
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS idx_playbooks_role ON playbooks (role, status);
"""


class PlaybookStore:
    def __init__(self, db_path: str, seed_dir: str | Path = "config/playbooks", event_store: Any = None):
        self.db_path, self.seed_dir, self.event_store = db_path, Path(seed_dir), event_store
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def _rows(self, sql: str, args=()) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def _event(self, agent: str, kind: str, payload: Dict[str, Any]) -> None:
        if self.event_store is not None:
            self.event_store.append(agent, kind, payload)

    def active(self, role: str) -> Optional[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM playbooks WHERE role = ? AND status = 'ACTIVE' ORDER BY version DESC LIMIT 1",
                          (role,))
        if rows:
            return rows[0]
        seed = self.seed_dir / f"{role}.md"
        if not seed.exists():
            return None
        text = seed.read_text(encoding="utf-8").strip()[:MAX_PLAYBOOK_CHARS]
        row = {"id": str(uuid.uuid4()), "role": role, "version": 1, "text": text, "status": "ACTIVE",
               "proposed_by": "seed", "reason": "initial version", "diff": None, "created_at": time.time(),
               "decided_at": time.time()}
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO playbooks VALUES (:id,:role,:version,:text,:status,:proposed_by,:reason,:diff,"
                         ":created_at,:decided_at)", row)
        return row

    def propose(self, role: str, agent: str, reason: str, add_rule: Optional[str] = None,
                remove_rule: Optional[str] = None) -> Dict[str, Any]:
        cur = self.active(role)
        if cur is None:
            raise ValueError(f"no playbook for role {role}")
        lines = cur["text"].splitlines()
        if add_rule:
            rule = " ".join(add_rule.split())[:MAX_RULE_CHARS]
            if any(rule.lower() in l.lower() for l in lines):
                raise ValueError("rule already in the playbook")
            lines.append(f"- {rule}")
        if remove_rule:
            target = " ".join(remove_rule.split()).lower()
            keep = [l for l in lines if target not in l.lower()]
            if len(keep) == len(lines):
                raise ValueError("rule to remove not found")
            lines = keep
        new_text = "\n".join(lines)
        if len(new_text) > MAX_PLAYBOOK_CHARS:
            raise ValueError("playbook would become too long")
        diff = "\n".join(difflib.unified_diff(cur["text"].splitlines(), lines, f"{role} v{cur['version']}",
                                              f"{role} proposed", lineterm=""))
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE playbooks SET status = 'SUPERSEDED', decided_at = ? WHERE role = ? AND status = 'PROPOSED'",
                         (time.time(), role))
            row = {"id": str(uuid.uuid4()), "role": role, "version": None, "text": new_text, "status": "PROPOSED",
                   "proposed_by": agent, "reason": " ".join(reason.split())[:300], "diff": diff,
                   "created_at": time.time(), "decided_at": None}
            conn.execute("INSERT INTO playbooks VALUES (:id,:role,:version,:text,:status,:proposed_by,:reason,:diff,"
                         ":created_at,:decided_at)", row)
        self._event(agent, "PLAYBOOK_PROPOSED", {"id": row["id"], "role": role, "reason": row["reason"], "diff": diff})
        return row

    def pending(self) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM playbooks WHERE status = 'PROPOSED' ORDER BY created_at")

    def decide(self, proposal_id: str, approve: bool, decided_by: str = "OPERATOR") -> Dict[str, Any]:
        rows = self._rows("SELECT * FROM playbooks WHERE id = ?", (proposal_id,))
        if not rows or rows[0]["status"] != "PROPOSED":
            raise ValueError("no open proposal with this id")
        row = rows[0]
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            if approve:
                version = (conn.execute("SELECT MAX(version) FROM playbooks WHERE role = ?", (row["role"],))
                           .fetchone()[0] or 0) + 1
                conn.execute("UPDATE playbooks SET status = 'ARCHIVED' WHERE role = ? AND status = 'ACTIVE'", (row["role"],))
                conn.execute("UPDATE playbooks SET status = 'ACTIVE', version = ?, decided_at = ? WHERE id = ?",
                             (version, now, proposal_id))
                row.update(status="ACTIVE", version=version)
            else:
                conn.execute("UPDATE playbooks SET status = 'REJECTED', decided_at = ? WHERE id = ?", (now, proposal_id))
                row.update(status="REJECTED")
        self._event(decided_by, "PLAYBOOK_APPROVED" if approve else "PLAYBOOK_REJECTED",
                    {"id": proposal_id, "role": row["role"], "version": row.get("version")})
        return row

    def history(self, role: str) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM playbooks WHERE role = ? ORDER BY created_at", (role,))
