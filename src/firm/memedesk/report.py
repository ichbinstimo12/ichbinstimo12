"""Daten fuer den App-Tab "Memecoin Desk" (nur lesend, aus Zustand, Journal und Ereignissen)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from src.firm.memedesk import live_gate

MEME_TYPES = ["MEME_TRADE_OPEN", "MEME_TRADE_PARTIAL", "MEME_TRADE_CLOSE", "MEME_VETO", "MEME_SKIP", "MEME_LIMIT",
              "MEME_ERROR", "MEME_NARRATIVE", "MEME_WALLET_SWAP", "MEME_COACH_CHANGE", "MEME_COACH_REVIEW",
              "MEME_KILL_REQUESTED", "MEME_RESUMED", "MEME_MODE_REQUEST", "MEME_SCAN"]


def _p(e) -> Dict[str, Any]:
    try:
        return json.loads(e.get("payload_json") or "{}")
    except ValueError:
        return {}


def build(event_store, data_dir: Path = Path("data/memedesk"), journal: Path = Path("archiv/memedesk/journal.jsonl"),
          limits: Dict[str, Any] | None = None, now: float | None = None) -> Dict[str, Any]:
    now = now or time.time()
    st_path = data_dir / "state.json"
    state = json.loads(st_path.read_text(encoding="utf-8")) if st_path.exists() else None
    rows = [json.loads(l) for l in journal.read_text(encoding="utf-8").splitlines() if l.strip()] if journal.exists() else []
    closes = [r for r in rows if r.get("type") == "CLOSE"]
    pnl_week = sum(float(r.get("pnl_sol") or 0) for r in closes if now - float(r.get("ts") or 0) <= 7 * 86400)
    start = float((limits or {}).get("capital", {}).get("paper_start_sol") or 0) or None
    mark = float(state.get("equity_mark") or state.get("equity") or 0) if state else None
    watch = event_store.get_events(types=["MEME_WATCHLIST"], last=1)
    log = [{"ts": e["ts"], "agent": str(e.get("agent_id") or "").replace("MEME_", ""), "type": e["type"],
            **{k: v for k, v in _p(e).items() if k not in ("metrics", "snapshot")}}
           for e in event_store.get_events(types=MEME_TYPES, last=150)]
    versions = sorted(data_dir.glob("params_v*.json"))
    coach = [json.loads(v.read_text(encoding="utf-8")) for v in versions[-5:]]
    return {
        "running": state is not None,
        "state": None if state is None else {k: state.get(k) for k in ("mode", "equity", "equity_mark", "peak", "day_start",
                                                                        "paused_day", "halted", "closed_trades")},
        "killed": (data_dir / "halt").exists(),
        "positions": list((state or {}).get("positions", {}).values()),
        "pnl": {"today": None if not state or not state.get("day_start") else round(mark - float(state["day_start"]), 6),
                "week_realized": round(pnl_week, 6),
                "total": None if mark is None or start is None else round(mark - start, 6), "unit": "SOL"},
        "trades_closed": len(closes),
        "watchlist": (_p(watch[-1]).get("tokens") if watch else []) or [],
        "log": list(reversed(log)),
        "coach": [{k: c.get(k) for k in ("version", "created", "diff", "backtest")} for c in coach],
        "live_gate": live_gate.paper_record(journal),
    }
