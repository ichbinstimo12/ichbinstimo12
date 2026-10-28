import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("AgentMemory")

# Hard cap per agent: keeps prompts small and prevents the agent from drifting
# into an ever-growing, self-reinforcing block of notes.
MAX_LESSONS_PER_AGENT = 20
LESSON_TTL_DAYS = 30
MIN_LESSON_CHARS = 15
MAX_LESSON_CHARS = 400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    lesson_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    lesson TEXT NOT NULL,
    normalized TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.6,
    evidence_task_id TEXT,
    source TEXT NOT NULL DEFAULT 'reflection',
    created_at TEXT NOT NULL,
    expires_at TEXT,
    applied_count INTEGER NOT NULL DEFAULT 0,
    last_applied_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lessons_unique ON lessons (agent_id, normalized);
CREATE INDEX IF NOT EXISTS idx_lessons_agent ON lessons (agent_id, confidence DESC, created_at DESC);
"""


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())[:MAX_LESSON_CHARS]


_POS = {"win", "wins", "won", "winning", "profitable", "profit", "works", "worked", "positive", "higher", "above",
        "increase", "increases", "bullish", "succeeds", "succeeded", "helps", "helped", "better", "gains"}
_NEG = {"lose", "loses", "lost", "losing", "unprofitable", "loss", "losses", "fails", "failed", "negative", "lower",
        "below", "decrease", "decreases", "bearish", "hurts", "hurt", "worse", "rejected"}
_NEGATORS = {"not", "never", "no", "dont", "doesnt", "didnt", "isnt", "wasnt"}
_STOP = {"the", "and", "with", "when", "were", "was", "are", "for", "that", "this", "than", "from", "after", "before",
         "during", "into", "over", "under", "trades", "trade", "entries"}


def _tokens(text: str) -> List[str]:
    return [w.strip(".,;:!?()\"'").replace("'", "") for w in text.lower().split()]


def polarity(text: str) -> int:
    """+1 positive claim, -1 negative claim, 0 neutral/mixed. A negator flips the sign."""
    toks = _tokens(text)
    pos, neg = any(t in _POS for t in toks), any(t in _NEG for t in toks)
    if pos == neg:
        return 0
    sign = 1 if pos else -1
    return -sign if any(t in _NEGATORS for t in toks) else sign


def subject(text: str) -> set:
    """Content words that identify what a lesson is about (market, strategy, condition)."""
    return {t for t in _tokens(text) if len(t) > 3 and t not in _POS | _NEG | _NEGATORS | _STOP}


def contradicts(a: str, b: str) -> bool:
    """Same subject (>= 2 shared content words), opposite claim."""
    pa, pb = polarity(a), polarity(b)
    return pa != 0 and pb != 0 and pa != pb and len(subject(a) & subject(b)) >= 2


class LessonStore:
    """
    Semantic memory of the Brain-Zone: what an agent learned from its own work.

    This is the mechanism behind "the agent improves itself": after every task
    the agent reflects and writes short lessons, and those lessons are injected
    into its next prompt. It learns behaviour, not code.

    Hard boundaries (enforced here, not by the LLM):
      - lessons are plain text, never executed
      - a lesson can never change role, risk limits or money-zone behaviour
      - the store is capped per agent and pruned by confidence/recency
    """

    def __init__(self, db_path: str, max_per_agent: int = MAX_LESSONS_PER_AGENT, ttl_days: int = LESSON_TTL_DAYS):
        self.db_path = db_path
        self.max_per_agent = max_per_agent
        self.ttl_days = ttl_days
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            # Lightweight migration for databases created before expiry existed.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(lessons)").fetchall()}
            if "expires_at" not in columns:
                conn.execute("ALTER TABLE lessons ADD COLUMN expires_at TEXT")
            if "contradicts" not in columns:
                conn.execute("ALTER TABLE lessons ADD COLUMN contradicts TEXT")
            conn.commit()
        self.purge_expired()

    def add(
        self,
        agent_id: str,
        lesson: str,
        confidence: float = 0.6,
        evidence_task_id: Optional[str] = None,
        source: str = "reflection",
    ) -> Optional[Dict[str, Any]]:
        """
        Store one lesson. Returns the created row, or None if the lesson was
        empty, too short, or an exact duplicate of something already known
        (duplicates only raise the existing confidence).
        """
        text = " ".join(str(lesson or "").split()).strip(" -*\t")
        if len(text) < MIN_LESSON_CHARS:
            return None
        if text.upper() in ("NONE", "N/A", "NOTHING", "NONE."):
            return None
        text = text[:MAX_LESSON_CHARS]
        normalized = _normalize(text)
        confidence = max(0.1, min(1.0, float(confidence)))
        lesson_id = str(uuid.uuid4())
        ts = datetime.utcnow().isoformat()
        expires_at = (datetime.utcnow() + timedelta(days=self.ttl_days)).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO lessons "
                "(lesson_id, agent_id, lesson, normalized, confidence, evidence_task_id, source, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (lesson_id, agent_id, text, normalized, confidence, evidence_task_id, source, ts, expires_at),
            )
            created = cursor.rowcount > 0
            if not created:
                # Re-learning the same thing = stronger belief, refreshed lifetime.
                conn.execute(
                    "UPDATE lessons SET confidence = MIN(1.0, confidence + 0.05), expires_at = ? "
                    "WHERE agent_id = ? AND normalized = ?",
                    (expires_at, agent_id, normalized),
                )
            conn.commit()

        if not created:
            return None

        # Widersprueche markieren statt ueberschreiben: beide Lektionen bleiben, beide tragen den Verweis.
        conflicts = [l for l in self.recent(agent_id, limit=self.max_per_agent)
                     if l["lesson_id"] != lesson_id and contradicts(text, l["lesson"])]
        if conflicts:
            with sqlite3.connect(self.db_path) as conn:
                for other in conflicts:
                    self._link(conn, lesson_id, other["lesson_id"])
                    self._link(conn, other["lesson_id"], lesson_id)
                conn.commit()
        self.prune(agent_id)
        logger.info(f"Agent {agent_id} learned: {text[:100]} (confidence {confidence:.2f})")
        return {
            "lesson_id": lesson_id,
            "agent_id": agent_id,
            "lesson": text,
            "confidence": confidence,
            "evidence_task_id": evidence_task_id,
            "source": source,
            "created_at": ts,
            "expires_at": expires_at,
            "applied_count": 0,
            "contradicts": [c["lesson_id"] for c in conflicts],
        }

    @staticmethod
    def _link(conn, lesson_id: str, other_id: str) -> None:
        row = conn.execute("SELECT contradicts FROM lessons WHERE lesson_id = ?", (lesson_id,)).fetchone()
        ids = [i for i in ((row[0] if row else None) or "").split(",") if i]
        if other_id not in ids:
            ids.append(other_id)
        conn.execute("UPDATE lessons SET contradicts = ? WHERE lesson_id = ?", (",".join(ids), lesson_id))

    def recent(self, agent_id: str, limit: int = 6) -> List[Dict[str, Any]]:
        """Highest-confidence, most recent, non-expired lessons for this agent."""
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM lessons WHERE agent_id = ? AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY confidence DESC, created_at DESC LIMIT ?",
                (agent_id, now, max(1, limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def relevant(self, agent_id: str, text: str, limit: int = 6) -> List[Dict[str, Any]]:
        """Lessons most related to the task text (keyword overlap), ties broken by confidence and recency."""
        candidates = self.recent(agent_id, limit=self.max_per_agent)
        words = {w for w in _normalize(text or "").replace(",", " ").replace(".", " ").split() if len(w) > 3}

        def score(lesson: Dict[str, Any]) -> float:
            lw = set(lesson["normalized"].replace(",", " ").replace(".", " ").split())
            return len(words & lw) + float(lesson["confidence"])

        return sorted(candidates, key=score, reverse=True)[:max(1, limit)]

    def record_applied(self, lesson_ids: List[str]) -> None:
        ids = [i for i in (lesson_ids or []) if i]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        now = datetime.utcnow()
        # Being shown in a prompt is not a confirmation: only re-learning the same lesson (add) extends its life.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"UPDATE lessons SET applied_count = applied_count + 1, last_applied_at = ? "
                f"WHERE lesson_id IN ({placeholders})",
                (now.isoformat(), *ids),
            )
            conn.commit()

    def purge_expired(self) -> int:
        """Remove lessons that were not confirmed (re-used) within their lifetime."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM lessons WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (datetime.utcnow().isoformat(),),
            )
            conn.commit()
            return cursor.rowcount

    def prune(self, agent_id: str) -> int:
        """Keep only the strongest lessons for this agent."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM lessons WHERE agent_id = ? AND lesson_id NOT IN ("
                "  SELECT lesson_id FROM lessons WHERE agent_id = ? "
                "  ORDER BY confidence DESC, created_at DESC LIMIT ?"
                ")",
                (agent_id, agent_id, self.max_per_agent),
            )
            conn.commit()
            return cursor.rowcount

    def count(self, agent_id: Optional[str] = None) -> int:
        with sqlite3.connect(self.db_path) as conn:
            if agent_id:
                row = conn.execute("SELECT COUNT(*) FROM lessons WHERE agent_id = ?", (agent_id,)).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()
        return int(row[0]) if row else 0

    def delete(self, lesson_id: str) -> bool:
        """Operator removes a lesson (UI). References in other lessons are cleaned up."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM lessons WHERE lesson_id = ?", (lesson_id,))
            for lid, refs in conn.execute("SELECT lesson_id, contradicts FROM lessons WHERE contradicts LIKE ?",
                                          (f"%{lesson_id}%",)).fetchall():
                ids = [i for i in (refs or "").split(",") if i and i != lesson_id]
                conn.execute("UPDATE lessons SET contradicts = ? WHERE lesson_id = ?", (",".join(ids) or None, lid))
            conn.commit()
        return cur.rowcount > 0

    def all_for_agent(self, agent_id: str) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM lessons WHERE agent_id = ? ORDER BY created_at ASC", (agent_id,)
            ).fetchall()
        return [dict(r) for r in rows]
