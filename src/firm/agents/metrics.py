"""Messbares Lernen: Kennzahlen pro Agent, ausschliesslich aus dem Event-Log berechnet.

Keine "Erfahrungspunkte". Gezaehlt wird, was wirklich passiert ist:
  tasks          abgeschlossene Aufgaben
  valid_rate     Anteil gueltiger Antworten (JSON + keine Widersprueche zu den Fakten)
  invalid        verworfene Antworten (AGENT_INVALID)
  llm_failures   Modell nicht erreichbar / abgelehnt
  actions        ausgefuehrte Aktionen, rejected_actions abgelehnte Aktionsvorschlaege
  tool_ok_rate   Anteil erfolgreicher Werkzeugaufrufe
  tokens         verbrauchte Tokens (laut Provider)
Die Zeitreihe (je Tag) zeigt, ob ein Agent besser wird.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

TYPES = ["RESULT", "AGENT_INVALID", "ACTION", "ACTION_REJECTED", "TOOL_CALL", "LEARNED"]


def _payload(e) -> Dict[str, Any]:
    try:
        return json.loads(e.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}


def _since(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None).isoformat()


def agent_metrics(event_store, days: float = 7.0, agent: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for e in event_store.get_events(after_ts=_since(days), types=TYPES):
        name = e.get("agent_id") or "?"
        if agent and name != agent:
            continue
        m = out.setdefault(name, {"tasks": 0, "valid": 0, "invalid": 0, "llm_failures": 0, "actions": 0,
                                  "rejected_actions": 0, "tool_calls": 0, "tool_ok": 0, "tokens": 0, "lessons": 0,
                                  "daily": {}})
        day = str(e.get("ts"))[:10]
        d = m["daily"].setdefault(day, {"tasks": 0, "valid": 0})
        p = _payload(e)
        t = e["type"]
        if t == "RESULT":
            m["tasks"] += 1; d["tasks"] += 1
            m["tokens"] += int(p.get("tokens") or 0)
            if p.get("status") == "success":
                m["valid"] += 1; d["valid"] += 1
            elif p.get("status") == "llm_unavailable":
                m["llm_failures"] += 1
        elif t == "AGENT_INVALID":
            m["invalid"] += 1
        elif t == "ACTION":
            m["actions"] += 1
        elif t == "ACTION_REJECTED":
            m["rejected_actions"] += 1
        elif t == "TOOL_CALL":
            m["tool_calls"] += 1
            m["tool_ok"] += 1 if p.get("ok") else 0
        elif t == "LEARNED":
            m["lessons"] += len(p.get("lessons") or [])
    for m in out.values():
        answered = m["tasks"] - m["llm_failures"]
        m["valid_rate"] = round(m["valid"] / answered, 3) if answered else None
        m["tool_ok_rate"] = round(m["tool_ok"] / m["tool_calls"], 3) if m["tool_calls"] else None
        for d in m["daily"].values():
            d["valid_rate"] = round(d["valid"] / d["tasks"], 3) if d["tasks"] else None
    return out


def tokens_today(event_store, agent: Optional[str] = None) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None).isoformat()
    total = 0
    for e in event_store.get_events(after_ts=start, types=["RESULT"]):
        if agent and e.get("agent_id") != agent:
            continue
        total += int(_payload(e).get("tokens") or 0)
    return total
