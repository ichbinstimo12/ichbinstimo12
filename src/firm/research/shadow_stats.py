"""Schattenbuch-Auswertung: Was haetten die Scanner-Signale gebracht, waeren sie gehandelt worden?

Grundlage sind ausschliesslich SHADOW_RESULT-Ereignisse des MarketScanners (Spread bereits abgezogen). Das ist ein
BELEG, keine Freigabe: Gehandelt wird weiterhin nur, was die Forschung (Walk-Forward, Trial-Counter) freigibt.
Gute Schatten-Ergebnisse loesen nur einen Forschungslauf fuer diesen Markt aus.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional

MIN_SHADOW_TRADES = 30          # darunter ist der Mittelwert reines Rauschen
MIN_EXPECTANCY_R = 0.10         # nach Spread; Puffer fuer Slippage/Finanzierung, die das Schattenbuch nicht kennt
MIN_T_STAT = 1.5                # grobe Signifikanz (Mittelwert / Standardfehler)


def _payload(ev: Dict[str, Any]) -> Dict[str, Any]:
    p = ev.get("payload")
    if isinstance(p, dict):
        return p
    try:
        return json.loads(ev.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}


def summarize(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Gruppiert nach (Markt, Strategie, Zeitrahmen): Anzahl, Erwartungswert in R, Trefferquote, t-Wert."""
    groups: Dict[tuple, List[float]] = {}
    for ev in events:
        p = _payload(ev)
        if p.get("outcome") == "invalid" or not isinstance(p.get("r"), (int, float)) or not p.get("epic"):
            continue
        groups.setdefault((p["epic"], p.get("strategy") or "?", p.get("resolution") or "?"), []).append(float(p["r"]))
    out = []
    for (epic, strategy, res), rs in groups.items():
        n = len(rs)
        mean = sum(rs) / n
        sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
        out.append({"epic": epic, "strategy": strategy, "resolution": res, "n": n,
                    "expectancy_r": round(mean, 3), "total_r": round(sum(rs), 2),
                    "win_rate": round(sum(1 for r in rs if r > 0) / n, 3),
                    "t_stat": round(mean / (sd / math.sqrt(n)), 2) if sd > 0 else None})
    return sorted(out, key=lambda g: (-g["n"], -g["expectancy_r"]))


def promising(summary: List[Dict[str, Any]], verdicts: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Gruppen mit genug Beleg fuer einen Forschungslauf - nur Maerkte, die noch keine Freigabe (PASS) haben."""
    verdicts = verdicts or {}
    return [g for g in summary
            if g["n"] >= MIN_SHADOW_TRADES and g["expectancy_r"] >= MIN_EXPECTANCY_R
            and g["t_stat"] is not None and g["t_stat"] >= MIN_T_STAT
            and (verdicts.get(g["epic"]) or {}).get("verdict") != "PASS"]
