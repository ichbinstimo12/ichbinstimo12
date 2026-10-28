import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
import json

class EventStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    ts TIMESTAMP NOT NULL,
                    agent_id TEXT,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    correlation_id TEXT
                )
            """)
            # Trigger to prevent UPDATE or DELETE on the events table
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS prevent_event_update
                BEFORE UPDATE ON events
                BEGIN
                    SELECT RAISE(FAIL, 'Events are immutable and cannot be updated');
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS prevent_event_delete
                BEFORE DELETE ON events
                BEGIN
                    SELECT RAISE(FAIL, 'Events are immutable and cannot be deleted');
                END;
            """)
            conn.commit()

    def append(self, agent_id: Optional[str], event_type: str, payload: Dict[str, Any], correlation_id: Optional[str] = None) -> str:
        event_id = str(uuid.uuid4())
        ts = datetime.utcnow().isoformat()
        payload_json = json.dumps(payload)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO events (id, ts, agent_id, type, payload_json, correlation_id) VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, ts, agent_id, event_type, payload_json, correlation_id)
            )
            conn.commit()
        return event_id

    def get_events(self, correlation_id: Optional[str] = None, after_ts: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            clauses = []
            params: List[Any] = []
            if correlation_id:
                clauses.append("correlation_id = ?")
                params.append(correlation_id)
            if after_ts:
                clauses.append("ts > ?")
                params.append(after_ts)

            query = "SELECT * FROM events"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY ts ASC"
            if limit:
                query += " LIMIT ?"
                params.append(limit)

            cursor.execute(query, tuple(params))
            return [dict(row) for row in cursor.fetchall()]

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
