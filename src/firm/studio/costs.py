"""KI-Kosten aus dem Ereignisprotokoll (RESULT-Ereignisse aller Agenten und des Studios).

Studio-Aufrufe speichern Eingabe-/Ausgabe-Tokens und den Betrag exakt. Bei den uebrigen Agenten ist nur die
Summe der Tokens bekannt: dort wird mit 80 % Eingabe / 20 % Ausgabe geschaetzt und als Schaetzung ausgewiesen.
Ollama und unbekannte Modelle kosten 0 (lokal).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

INPUT_SHARE = 0.8


def price_of(model: Optional[str], prices: Dict[str, Dict[str, float]]) -> Optional[Dict[str, float]]:
    if not model:
        return None
    if model in prices:
        return prices[model]
    for name, p in prices.items():         # "claude-sonnet-5-5-xyz" -> Basismodell
        if str(model).startswith(name):
            return p
    return None


def call_cost(model: Optional[str], input_tokens: int, output_tokens: int, prices: Dict[str, Dict[str, float]]) -> float:
    p = price_of(model, prices)
    if p is None:
        return 0.0
    return input_tokens / 1e6 * float(p["input"]) + output_tokens / 1e6 * float(p["output"])


def ai_cost(event_store, prices: Dict[str, Dict[str, float]], since: datetime,
            agent_prefix: Optional[str] = None) -> Dict[str, Any]:
    exact = estimated = 0.0
    tokens = 0
    for e in event_store.get_events(after_ts=since.isoformat(), types=["RESULT"]):
        if agent_prefix and not str(e.get("agent_id") or "").startswith(agent_prefix):
            continue
        try:
            p = json.loads(e.get("payload_json") or "{}")
        except ValueError:
            continue
        tokens += int(p.get("tokens") or 0)
        if isinstance(p.get("cost_usd"), (int, float)):
            exact += float(p["cost_usd"])
        elif p.get("tokens"):
            t = int(p["tokens"])
            estimated += call_cost(p.get("model"), int(t * INPUT_SHARE), t - int(t * INPUT_SHARE), prices)
    return {"usd": round(exact + estimated, 4), "exact_usd": round(exact, 4), "estimated_usd": round(estimated, 4),
            "tokens": tokens}


def month_start(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def days_ago(n: float, now: Optional[datetime] = None) -> datetime:
    return (now or datetime.utcnow()) - timedelta(days=n)
