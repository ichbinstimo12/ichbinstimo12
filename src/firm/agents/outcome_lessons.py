"""Lernen aus echten Ergebnissen (deterministisch, keine Tokens).

Die Reflexions-Lektionen der Agenten kommen vom LLM und sind oft allgemein (werden dann verworfen) oder fehlen ganz.
Hier entstehen Lektionen aus gemessenen Zahlen: Live-Trades, Schattenbuch, vor dem Senden verworfene Signale und
Slippage. Jede Lektion hat einen Beleg (die juengste Ereignis-ID der Gruppe) und landet bei den Rollen, die damit
arbeiten. Solange die Zahlen gleich bleiben, wird sie bestaetigt (laengere Lebensdauer); aendern sie sich, ersetzt
die neue Messung die alte. Lektionen sind Hinweise im Prompt - sie aendern nie Limits, Modus oder Freigaben.
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("OutcomeLessons")

PREFIX = "[measured "
WINDOW_DAYS = 60
MIN_TRADES = 5
MIN_SHADOW = 30
MIN_BLOCKS = 5
MAX_PER_AGENT = 4            # Messungen pro Agent - der Rest des Gedaechtnisses bleibt den eigenen Lektionen

TRADE_ROLES = ("ExecutionTrader", "RiskOfficer", "HeadOfResearch", "CEO")
SMC_ROLES = ("SMCTrader", "SMCAnalyst")
SHADOW_ROLES = ("HeadOfResearch",)
EXEC_ROLES = ("ExecutionTrader", "RiskOfficer")


def _p(e: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(e.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}


def _groups(events: Iterable[Dict[str, Any]], key) -> Dict[Any, List[Tuple[str, Dict[str, Any]]]]:
    out: Dict[Any, List[Tuple[str, Dict[str, Any]]]] = defaultdict(list)
    for e in events:
        p = _p(e)
        k = key(p)
        if k is not None:
            out[k].append((e["id"], p))
    return out


def measurements(event_store, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """-> [{key, text, roles, n, evidence}] aus den Ereignissen der letzten WINDOW_DAYS Tage."""
    since = ((now or datetime.utcnow()) - timedelta(days=WINDOW_DAYS)).isoformat()
    ev = lambda t: event_store.get_events(types=[t], after_ts=since)
    out: List[Dict[str, Any]] = []

    live = _groups(ev("TRADE_RESULT"), lambda p: (p.get("epic"), p.get("strategy") or "?")
                   if p.get("epic") and isinstance(p.get("r"), (int, float)) else None)
    for (epic, strat), rows in live.items():
        rs = [p["r"] for _, p in rows]
        if len(rs) < MIN_TRADES:
            continue
        mean, wr = sum(rs) / len(rs), sum(1 for r in rs if r > 0) / len(rs)
        verdict = "losing money - do not trust this setup" if mean < 0 else "making money so far"
        roles = TRADE_ROLES + (SMC_ROLES if str(strat).startswith("smc") else ())
        out.append({"key": f"live {epic}/{strat}", "n": len(rs), "evidence": rows[-1][0], "roles": roles,
                    "text": f"{len(rs)} live trades in {WINDOW_DAYS} days: avg {mean:+.2f} R, hit rate {wr:.0%} "
                            f"({verdict})"})

    shadow = _groups(ev("SHADOW_RESULT"), lambda p: (p.get("epic"), p.get("strategy") or "?", p.get("resolution") or "?")
                     if p.get("epic") and isinstance(p.get("r"), (int, float)) and p.get("outcome") != "invalid" else None)
    for (epic, strat, res), rows in shadow.items():
        rs = [p["r"] for _, p in rows]
        if len(rs) < MIN_SHADOW:
            continue
        mean = sum(rs) / len(rs)
        roles = SHADOW_ROLES + (("SMCAnalyst",) if str(strat).startswith("smc") else ())
        out.append({"key": f"shadow {epic}/{strat}/{res}", "n": len(rs), "evidence": rows[-1][0], "roles": roles,
                    "text": f"{len(rs)} untraded shadow signals: avg {mean:+.2f} R after spread "
                            f"({'worth researching' if mean > 0.1 else 'no edge visible'})"})

    blocked = _groups(ev("SIGNAL_BLOCKED"), lambda p: p.get("epic") if p.get("stage") == "entry-quality" else None)
    for epic, rows in blocked.items():
        if len(rows) < MIN_BLOCKS:
            continue
        kinds = Counter(str(p.get("reason") or "").split(" ")[0] for _, p in rows)
        main = kinds.most_common(1)[0][0] or "?"
        out.append({"key": f"blocked {epic}", "n": len(rows), "evidence": rows[-1][0], "roles": EXEC_ROLES,
                    "text": f"{len(rows)} signals blocked before sending, mostly '{main}' "
                            "(costs/price drift too high at those times)"})

    fills = _groups(ev("FILL_QUALITY"), lambda p: p.get("epic") if isinstance(p.get("slippage_r"), (int, float)) else None)
    for epic, rows in fills.items():
        if len(rows) < MIN_BLOCKS:
            continue
        avg = sum(p["slippage_r"] for _, p in rows) / len(rows)
        out.append({"key": f"fills {epic}", "n": len(rows), "evidence": rows[-1][0], "roles": ("ExecutionTrader",),
                    "text": f"average slippage {avg:+.2f} R over {len(rows)} fills"})
    return out


def refresh(event_store, lessons, now: Optional[datetime] = None) -> Dict[str, int]:
    """Messungen in die Gedaechtnisse schreiben: bestaetigen, ersetzen, begrenzen."""
    stats = {"added": 0, "confirmed": 0, "replaced": 0}
    per_agent: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for m in measurements(event_store, now):
        for role in m["roles"]:
            per_agent[role].append(m)
    for agent, ms in per_agent.items():
        ms = sorted(ms, key=lambda m: -m["n"])[:MAX_PER_AGENT]
        keep = {f"{PREFIX}{m['key']}] {m['text']}" for m in ms}
        for old in lessons.recent(agent, limit=200):
            if old["lesson"].startswith(PREFIX) and old["lesson"] not in keep:
                lessons.delete(old["lesson_id"])
                stats["replaced"] += 1
        for m in ms:
            text = f"{PREFIX}{m['key']}] {m['text']}"
            conf = min(0.95, 0.5 + m["n"] / 100)
            created = lessons.add(agent, text, confidence=conf, evidence_task_id=m["evidence"], source="outcome")
            stats["added" if created else "confirmed"] += 1
    return stats
