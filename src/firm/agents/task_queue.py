import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.firm.bus.event_store import EventStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TaskQueue")

PENDING = "PENDING"
RUNNING = "RUNNING"
DONE = "DONE"
FAILED = "FAILED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    created_by TEXT NOT NULL,
    assigned_to TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    result_json TEXT,
    correlation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks (assigned_to, status);
"""


@dataclass
class Task:
    task_id: str
    created_by: str
    assigned_to: str
    title: str
    description: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result_json: Optional[str] = None
    correlation_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TaskQueue:
    """
    Durable, SQLite-backed task queue for the Brain-Zone.

    The CEO creates tasks; agents pick up their pending tasks.
    Every transition is mirrored into the Event Store (audit trail).
    """

    def __init__(self, db_path: str, event_store: Optional[EventStore] = None):
        self.db_path = db_path
        self.event_store = event_store
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()

    def _log(self, agent_id: str, event_type: str, payload: Dict[str, Any], correlation_id: Optional[str] = None) -> None:
        if self.event_store is not None:
            self.event_store.append(agent_id, event_type, payload, correlation_id=correlation_id)

    def create_task(
        self,
        created_by: str,
        assigned_to: str,
        title: str,
        description: str,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        task_id = str(uuid.uuid4())
        ts = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO tasks (task_id, created_by, assigned_to, title, description, status, created_at, correlation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, created_by, assigned_to, title, description, PENDING, ts, correlation_id),
            )
            conn.commit()
        task = self.get_task(task_id)
        self._log(created_by, "TASK_CREATED", {
            "task_id": task_id,
            "assigned_to": assigned_to,
            "title": title,
        }, correlation_id=correlation_id or task_id)
        logger.info(f"Task created: {title} ({task_id}) -> {assigned_to}")
        return task

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def next_pending(self, agent_name: str) -> Optional[Dict[str, Any]]:
        """Atomically claim the oldest pending task for this agent."""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE assigned_to = ? AND status = ? ORDER BY created_at ASC LIMIT 1",
                (agent_name, PENDING),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            task_id = row["task_id"]
            conn.execute(
                "UPDATE tasks SET status = ?, started_at = ? WHERE task_id = ?",
                (RUNNING, datetime.utcnow().isoformat(), task_id),
            )
            conn.commit()
            claimed = self.get_task(task_id)
        self._log(agent_name, "TASK_STARTED", {"task_id": task_id, "title": claimed["title"] if claimed else ""}, correlation_id=task_id)
        return claimed

    def complete(self, task_id: str, result: Dict[str, Any]) -> None:
        ts = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, finished_at = ?, result_json = ? WHERE task_id = ?",
                (DONE, ts, json.dumps(result, default=str), task_id),
            )
            conn.commit()
        task = self.get_task(task_id)
        self._log(task["assigned_to"] if task else "SYSTEM", "TASK_COMPLETED", {
            "task_id": task_id,
            "result": result,
        }, correlation_id=task_id)

    def fail(self, task_id: str, error: str) -> None:
        ts = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, finished_at = ?, result_json = ? WHERE task_id = ?",
                (FAILED, ts, json.dumps({"error": error}), task_id),
            )
            conn.commit()
        task = self.get_task(task_id)
        self._log(task["assigned_to"] if task else "SYSTEM", "TASK_FAILED", {
            "task_id": task_id,
            "error": error,
        }, correlation_id=task_id)

    def list_tasks(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if status:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def count_by_status(self) -> Dict[str, int]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
        counts = {PENDING: 0, RUNNING: 0, DONE: 0, FAILED: 0}
        for status, n in rows:
            counts[status] = n
        return counts

    def pending_count(self) -> int:
        return self.count_by_status()[PENDING]

    def requeue_stale(self) -> int:
        """
        Crash recovery: any task left RUNNING by a previous process is put back
        to PENDING so it can be claimed again after a restart.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status = ?, started_at = NULL WHERE status = ?",
                (PENDING, RUNNING),
            )
            conn.commit()
            count = cursor.rowcount
        if count:
            self._log("SYSTEM", "TASKS_REQUEUED", {"count": count})
            logger.warning(f"Requeued {count} stale RUNNING task(s) after restart.")
        return count
