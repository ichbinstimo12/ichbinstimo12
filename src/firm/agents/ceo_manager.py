import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from src.firm.bus.event_store import EventStore

logger = logging.getLogger("CEOManager")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hiring_requests (
    request_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    justification TEXT NOT NULL,
    config_json TEXT NOT NULL,
    token_budget INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    decided_at REAL,
    applied INTEGER NOT NULL DEFAULT 0
);
"""


class RequestStatus(Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass
class HiringRequest:
    request_id: str
    role: str
    justification: str
    config_proposal: Dict[str, Any]
    token_budget: int
    status: RequestStatus = RequestStatus.PENDING


class CEOManager:
    """
    Hiring requests of the CEO agent. Stored in SQLite, so they survive restarts and the dashboard process (which
    approves) and the agent process (which loads the new agent) see the same requests. No agent is ever added
    without the operator's approval.
    """

    def __init__(self, event_store: EventStore, on_approved: Optional[Callable[[str], None]] = None):
        self.event_store = event_store
        self.db_path = event_store.db_path
        self.on_approved = on_approved  # same-process shortcut; other processes poll approved_unapplied()
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    @property
    def hiring_requests(self) -> Dict[str, HiringRequest]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT request_id, role, justification, config_json, token_budget, status "
                                "FROM hiring_requests ORDER BY created_at").fetchall()
        return {r[0]: HiringRequest(r[0], r[1], r[2], json.loads(r[3]), int(r[4]), RequestStatus(r[5])) for r in rows}

    def create_hiring_request(self, role: str, justification: str, config: Dict[str, Any], budget: int) -> str:
        req_id = str(uuid.uuid4())
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO hiring_requests VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0)",
                         (req_id, role, justification, json.dumps(config), int(budget), "PENDING", time.time()))
        self.event_store.append("CEO", "HIRING_REQUEST", {
            "request_id": req_id, "role": role, "justification": justification, "budget": budget})
        logger.info(f"CEO created hiring request {req_id} for role {role}")
        return req_id

    def _decide(self, request_id: str, status: RequestStatus) -> HiringRequest:
        req = self.hiring_requests.get(request_id)
        if req is None:
            raise ValueError("Request ID not found")
        if req.status != RequestStatus.PENDING:
            raise ValueError(f"Request already {req.status.value}")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE hiring_requests SET status = ?, decided_at = ? WHERE request_id = ?",
                         (status.value, time.time(), request_id))
        self.event_store.append("USER", "HIRING_APPROVAL", {"request_id": request_id, "role": req.role,
                                                            "status": status.value})
        req.status = status
        return req

    def approve_hire(self, request_id: str):
        req = self._decide(request_id, RequestStatus.APPROVED)
        logger.info(f"Hiring request {request_id} APPROVED by user.")
        if self.on_approved is not None:
            self.on_approved(req.role)
            self.mark_applied(request_id)

    def reject_hire(self, request_id: str):
        self._decide(request_id, RequestStatus.REJECTED)
        logger.info(f"Hiring request {request_id} REJECTED by user.")

    def approved_unapplied(self) -> List[HiringRequest]:
        with sqlite3.connect(self.db_path) as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT request_id FROM hiring_requests WHERE status = 'APPROVED' AND applied = 0").fetchall()]
        reqs = self.hiring_requests
        return [reqs[i] for i in ids if i in reqs]

    def mark_applied(self, request_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE hiring_requests SET applied = 1 WHERE request_id = ?", (request_id,))
