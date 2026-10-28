"""
Durable, human-readable archive of everything the firm did.

The SQLite event store is the immutable ledger the code runs on. The archive is
the *filing cabinet* next to it: plain JSON files a person can open, one folder
per agent, one folder per trading day. Agents read their own history back out of
it before they think, which is what turns "36 lessons" into actual experience.

Layout (root = ``archiv`` by default)::

    archiv/
      INDEX.md                     written by the curator: what is where
      trades/
        ledger.jsonl               append-only, one line per trade event
        2026-10-24/
          143900_BTCUSD_SELL_FILLED_a1b2c3.json
          150512_BTCUSD_SELL_CLOSED_a1b2c3.json
        compressed/
          2026-10.jsonl.gz         older days, gzipped by the curator
      agents/
        CEO/
          actions.jsonl            every thought / result / lesson
          tasks/
            2026-10-24T1439_ab12cd34.json
          tasks/compressed/
            2026-10.jsonl.gz
          summary.md               newest tasks, rewritten by the curator

Nothing in here can move money. It is write-only for the trading path and
read-only for the LLM.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("Archive")

DEFAULT_ROOT = os.getenv("FIRM_ARCHIVE_DIR", "archiv")
MAX_FILE_NAME = 80


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp from the event store; None if it is unusable."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _safe(value: Any, fallback: str = "unknown") -> str:
    """Filesystem-safe token (agent names, epics and ids end up in file names)."""
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-.")
    return text[:MAX_FILE_NAME] or fallback


def _short_id(value: Any) -> str:
    """Stable short id for file names. Capital ids share a long prefix, so a
    hash is used instead of a prefix slice (which would collide)."""
    text = str(value or "").strip()
    if not text:
        return "noid"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _same_trade(fill: Dict[str, Any], close: Dict[str, Any]) -> bool:
    """True when a deal-id-less close can only belong to this fill."""
    if str(fill.get("epic") or "").upper() != str(close.get("epic") or "").upper():
        return False
    fill_side = str(fill.get("side") or "").upper()
    close_side = str(close.get("side") or close.get("direction") or "").upper()
    if not fill_side or fill_side != close_side:
        return False
    try:
        return abs(float(fill.get("size")) - float(close.get("size"))) <= 1e-9
    except (TypeError, ValueError):
        return False


class Archive:
    """Filesystem archive: trades per day, tasks and actions per agent."""

    def __init__(self, root: Optional[str] = None):
        self.root = Path(root or DEFAULT_ROOT)
        self.trades_dir = self.root / "trades"
        self.agents_dir = self.root / "agents"
        self.compressed_dir = self.trades_dir / "compressed"
        self._ready = False

    # ------------------------------------------------------------- plumbing
    def ensure(self) -> None:
        if self._ready:
            return
        try:
            self.trades_dir.mkdir(parents=True, exist_ok=True)
            self.agents_dir.mkdir(parents=True, exist_ok=True)
            self.compressed_dir.mkdir(parents=True, exist_ok=True)
            readme = self.root / "README.md"
            if not readme.exists():
                readme.write_text(
                    "# Archive\n\n"
                    "Every trade and every agent task is filed here as plain JSON.\n\n"
                    "- `trades/<day>/` - one file per trade event (signal, fill, close, reject)\n"
                    "- `trades/ledger.jsonl` - the same events, append-only, one line each\n"
                    "- `trades/compressed/` - older days as `.jsonl.gz` (written by the curator)\n"
                    "- `agents/<Agent>/tasks/` - one file per completed task\n"
                    "- `agents/<Agent>/actions.jsonl` - thoughts, results and lessons\n"
                    "- `INDEX.md` - generated summary, rewritten by the cleaning agent\n\n"
                    "Agents read their own history from here before they think.\n"
                    "Nothing in this folder can place an order or change a risk limit.\n",
                    encoding="utf-8",
                )
            self._ready = True
        except Exception as e:  # never break trading because of a disk problem
            logger.error(f"Archive unavailable at {self.root}: {e}")

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> Optional[Path]:
        try:
            self.ensure()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            return path
        except Exception as e:
            logger.error(f"Could not write archive file {path}: {e}")
            return None

    @staticmethod
    def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.error(f"Could not append to {path}: {e}")

    @staticmethod
    def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            if not path.exists():
                return rows
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"Could not read {path}: {e}")
        return rows

    # --------------------------------------------------------------- trades
    def record_trade(self, kind: str, payload: Dict[str, Any]) -> Optional[Path]:
        """
        File one trade event (SIGNAL, FILLED, CLOSED, REJECTED).

        Called from the deterministic desk, so it must never raise: a full disk
        must not stop the firm from flattening a losing position.
        """
        now = _utcnow()
        recorded_at = payload.get("recorded_at")
        if recorded_at:
            now = _parse_ts(recorded_at) or now
        epic = _safe(payload.get("epic"), "NA")
        side = _safe(payload.get("side") or payload.get("direction"), "NA")
        deal = payload.get("deal_id") or payload.get("order_id") or payload.get("dealId")
        record = {
            "recorded_at": now.isoformat(),
            "kind": str(kind).upper(),
            "epic": payload.get("epic"),
            "side": payload.get("side") or payload.get("direction"),
            "size": payload.get("size"),
            "price": payload.get("price") or payload.get("filled_price"),
            "deal_id": deal,
            "reason": payload.get("reason"),
            "profit": payload.get("profit"),
            "profit_currency": payload.get("profit_currency"),
            "source": payload.get("source") or "trading_desk",
            "raw": payload,
        }
        name = f"{now.strftime('%H%M%S')}_{epic}_{side}_{record['kind']}_{_short_id(deal)}.json"
        path = self._write_json(self.trades_dir / now.strftime("%Y-%m-%d") / name, record)
        self._append_jsonl(self.trades_dir / "ledger.jsonl", record)
        return path

    def trade_events(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Newest-last list of trade events, compressed months included."""
        events: List[Dict[str, Any]] = []
        for gz in sorted(self.compressed_dir.glob("*.jsonl.gz")):
            events.extend(self._read_gzip_jsonl(gz))
        events.extend(self._read_jsonl(self.trades_dir / "ledger.jsonl"))
        events.sort(key=lambda r: str(r.get("recorded_at") or ""))
        return events[-limit:] if limit else events

    def trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Trade history for humans and for the LLM: one row per fill, enriched
        with its close when we can match it.

        Broker closes usually carry the fill's deal id. When they carry the
        position id instead (Capital.com does this), the close is linked only
        when exactly one open fill matches on epic, side and size — the same
        rule the backfill uses. We never guess between two identical fills.
        """
        events = self.trade_events(limit=0)
        fill_events = [e for e in events if e.get("kind") == "FILLED"]
        close_events = [e for e in events if e.get("kind") == "CLOSED"]

        matched: Dict[int, Dict[str, Any]] = {}
        used: set = set()

        by_deal: Dict[str, Dict[str, Any]] = {}
        for close in close_events:
            if close.get("deal_id"):
                by_deal.setdefault(str(close["deal_id"]), close)
        for i, fill in enumerate(fill_events):
            deal = fill.get("deal_id")
            close = by_deal.get(str(deal)) if deal else None
            if close is not None:
                matched[i] = close
                used.add(id(close))

        for close in close_events:
            if id(close) in used:
                continue
            candidates = [
                i for i, fill in enumerate(fill_events)
                if i not in matched and _same_trade(fill, close)
            ]
            if len(candidates) == 1:
                matched[candidates[0]] = close

        fills: List[Dict[str, Any]] = []
        for i, e in enumerate(fill_events):
            close = matched.get(i)
            fills.append({
                "deal_id": e.get("deal_id"),
                "time": e.get("recorded_at"),
                "epic": e.get("epic"),
                "side": e.get("side"),
                "size": e.get("size"),
                "entry": e.get("price"),
                "exit": (close or {}).get("price"),
                "profit": (close or {}).get("profit"),
                "profit_currency": (close or {}).get("profit_currency"),
                "closed_at": (close or {}).get("recorded_at"),
                "status": "closed" if close else "open",
                "close_reason": (close or {}).get("reason"),
            })
        fills.sort(key=lambda r: str(r.get("time") or ""))
        return fills[-limit:] if limit else fills

    def record_rejection(self, payload: Dict[str, Any]) -> Optional[Path]:
        return self.record_trade("REJECTED", payload)

    # --------------------------------------------------------------- backfill
    def backfill_from_events(self, event_store: Any, limit: int = 5000) -> int:
        """
        Import trades that were executed before this archive existed.

        Only unambiguous facts are imported: a fill is a fill, and a broker
        close is linked to an open fill only when exactly one fill matches on
        epic, direction and size. When the link is ambiguous we skip it rather
        than guess. No price or P/L is ever invented.
        """
        try:
            events = event_store.get_events(limit=limit)
        except Exception as e:
            logger.warning(f"Could not read event history for backfill: {e}")
            return 0

        existing = self.trade_events(limit=0)
        filled_ids = {
            str(e.get("deal_id")) for e in existing
            if e.get("kind") == "FILLED" and e.get("deal_id")
        }
        closed_ids = {
            str(e.get("deal_id")) for e in existing
            if e.get("kind") == "CLOSED" and e.get("deal_id")
        }

        written = 0
        for event in events:
            kind = str(event.get("type") or "")
            payload = _loads(event.get("payload_json"))
            ts = event.get("ts")

            if kind == "ORDER_FILLED":
                deal = payload.get("order_id") or payload.get("deal_id")
                if not deal or str(deal) in filled_ids:
                    continue
                if self.record_trade("FILLED", {
                    "epic": payload.get("epic"),
                    "side": payload.get("side"),
                    "size": payload.get("size"),
                    "price": payload.get("price"),
                    "deal_id": deal,
                    "reason": "backfilled from event history",
                    "source": "backfill",
                    "recorded_at": ts,
                }) is not None:
                    filled_ids.add(str(deal))
                    written += 1

            elif kind == "POSITION_CLOSED":
                deal = payload.get("deal_id") or self._match_open_fill(payload, closed_ids)
                if not deal or str(deal) in closed_ids:
                    continue
                if self.record_trade("CLOSED", {
                    "epic": payload.get("epic"),
                    "side": payload.get("direction"),
                    "size": payload.get("size"),
                    "deal_id": deal,
                    "reason": payload.get("reason") or "backfilled from event history",
                    "source": "backfill",
                    "recorded_at": ts,
                }) is not None:
                    closed_ids.add(str(deal))
                    written += 1

        if written:
            logger.info(f"Archive backfilled {written} trade event(s) from the event store.")
        return written

    def _match_open_fill(self, payload: Dict[str, Any], closed_ids: set) -> Optional[str]:
        """Link a deal-id-less broker close to its fill, only if unambiguous."""
        epic = str(payload.get("epic") or "").upper()
        side = str(payload.get("direction") or payload.get("side") or "").upper()
        size = payload.get("size")
        try:
            size = float(size)
        except (TypeError, ValueError):
            return None

        candidates = []
        for e in self.trade_events(limit=0):
            if e.get("kind") != "FILLED":
                continue
            deal = e.get("deal_id")
            if not deal or str(deal) in closed_ids:
                continue
            if str(e.get("epic") or "").upper() != epic:
                continue
            if str(e.get("side") or "").upper() != side:
                continue
            try:
                if abs(float(e.get("size")) - size) > 1e-9:
                    continue
            except (TypeError, ValueError):
                continue
            candidates.append(str(deal))

        return candidates[0] if len(candidates) == 1 else None

    # ---------------------------------------------------------------- agents
    def agent_dir(self, agent: str) -> Path:
        return self.agents_dir / _safe(agent)

    def record_task(
        self,
        agent: str,
        task: Dict[str, Any],
        result: Optional[Dict[str, Any]] = None,
        actions: Optional[Iterable[Dict[str, Any]]] = None,
        error: Optional[str] = None,
    ) -> Optional[Path]:
        """File one task with the actions the agent took while doing it."""
        now = _utcnow()
        task_id = str(task.get("task_id") or "notask")
        base = self.agent_dir(agent)
        record = {
            "recorded_at": now.isoformat(),
            "task_id": task_id,
            "agent": agent,
            "title": task.get("title"),
            "description": task.get("description"),
            "status": task.get("status") or ("failed" if error else "done"),
            "error": error,
            "result": result or {},
            "actions": [
                {
                    "type": a.get("type"),
                    "ts": a.get("ts"),
                    "payload": _loads(a.get("payload_json")),
                }
                for a in (actions or [])
            ],
        }
        name = f"{now.strftime('%Y-%m-%dT%H%M%S')}_{_short_id(task_id)}.json"
        path = self._write_json(base / "tasks" / name, record)

        self._append_jsonl(base / "actions.jsonl", {
            "recorded_at": now.isoformat(),
            "task_id": task_id,
            "title": task.get("title"),
            "status": record["status"],
            "result": str((result or {}).get("deliverable") or "")[:2000],
            "lessons": (result or {}).get("lessons_learned") or [],
            "error": error,
        })
        for action in record["actions"]:
            self._append_jsonl(base / "actions.jsonl", {
                "recorded_at": now.isoformat(),
                "task_id": task_id,
                "action": action.get("type"),
                "content": str(action.get("payload") or "")[:4000],
            })
        return path

    def agent_tasks(self, agent: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Newest-last task records for one agent (compressed history included)."""
        base = self.agent_dir(agent)
        rows: List[Dict[str, Any]] = []
        for gz in sorted((base / "tasks" / "compressed").glob("*.jsonl.gz")):
            rows.extend(self._read_gzip_jsonl(gz))
        for path in sorted((base / "tasks").glob("*.json")):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        rows.sort(key=lambda r: str(r.get("recorded_at") or ""))
        return rows[-limit:] if limit else rows

    # -------------------------------------------------------------- context
    def context_for(self, agent: str, tasks: int = 3, trades: int = 5) -> str:
        """
        Compact history block for the agent's prompt.

        Only facts that are already on disk: task titles/outcomes and real
        broker fills. No interpretation, no invented numbers.
        """
        lines: List[str] = []

        own = self.agent_tasks(agent, limit=tasks)
        if own:
            lines.append("Your last tasks (from your archive folder):")
            for t in own:
                deliverable = str((t.get("result") or {}).get("deliverable") or "").replace("\n", " ")
                lessons = (t.get("result") or {}).get("lessons_learned") or []
                lines.append(
                    f"- {str(t.get('recorded_at') or '')[:16]} {t.get('title')} -> "
                    f"{deliverable[:140] or t.get('status')}"
                    + (f" | learned: {'; '.join(lessons)[:120]}" if lessons else "")
                )
        else:
            lines.append("Your archive folder is empty: this is your first task.")

        history = self.trades(limit=trades)
        if history:
            lines.append("Firm trade history (real broker fills):")
            for t in history:
                pnl = t.get("profit")
                pnl_txt = f"{pnl:+.2f} {t.get('profit_currency') or ''}".strip() if isinstance(pnl, (int, float)) else "open"
                lines.append(
                    f"- {str(t.get('time') or '')[:16]} {t.get('side')} {t.get('size')} "
                    f"{t.get('epic')} @ {t.get('entry')} -> {t.get('exit') or 'open'} ({pnl_txt})"
                )
        else:
            lines.append("No trades have been archived yet.")

        return "\n".join(lines)

    def stats(self) -> Dict[str, Any]:
        self.ensure()
        trades = self.trades(limit=0)
        closed = [t for t in trades if t.get("status") == "closed"]
        reported = [t for t in closed if isinstance(t.get("profit"), (int, float))]
        realized = sum(t["profit"] for t in reported)
        per_agent: Dict[str, int] = {}
        for d in sorted(self.agents_dir.iterdir()) if self.agents_dir.exists() else []:
            if not d.is_dir():
                continue
            per_agent[d.name] = len(list((d / "tasks").glob("*.json"))) if (d / "tasks").exists() else 0

        size = 0
        for path in self.root.rglob("*") if self.root.exists() else []:
            if path.is_file():
                try:
                    size += path.stat().st_size
                except OSError:
                    pass

        return {
            "root": str(self.root),
            "trades_total": len(trades),
            "trades_closed": len(closed),
            "trades_with_profit": len(reported),
            "realized_profit": round(realized, 2),
            "tasks_per_agent": per_agent,
            "compressed_files": len(list(self.compressed_dir.glob("*.gz")))
            + len(list(self.agents_dir.glob("*/tasks/compressed/*.gz"))),
            "size_bytes": size,
        }

    # ---------------------------------------------------------------- gzip io
    @staticmethod
    def _read_gzip_jsonl(path: Path) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"Could not read compressed archive {path}: {e}")
        return rows

    @staticmethod
    def append_gzip_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
        """Append rows to a .jsonl.gz. Returns the number actually written."""
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with gzip.open(path, "at", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                written += 1
        return written


def _loads(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
