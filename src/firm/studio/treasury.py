"""Schatzkammer: rechnet, was die Firma verdient und kostet, und EMPFIEHLT, was mit dem Ueberschuss passiert.

Die Firma bewegt nie Geld (CLAUDE.md, Prinzip 5). Sie sagt dir, was laut deiner Regel in config/treasury.toml
sinnvoll waere; ueberweisen tust du und traegst es mit `firm treasury transferred <USD>` ein.

Reihenfolge der Verwendung (jede Stufe zuerst):
  1. Steuer-Ruecklage (tax_reserve_pct vom Netto-Umsatz)
  2. Reserve fuer die laufenden Kosten (runway_months x durchschnittliche Monatskosten)
  3. vom freien Rest hoechstens trading_topup_pct ins Trading - nur wenn eine Strategie die Pruefung bestanden hat
  4. der Rest bleibt als Polster stehen
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from src.firm.studio import costs, sales


def transferred_total(event_store) -> float:
    return round(sum(float(json.loads(e.get("payload_json") or "{}").get("usd") or 0)
                     for e in event_store.get_events(types=["TREASURY_TRANSFER_RECORDED"])), 2)


def trading_ready(gate_overview: Optional[Dict[str, Any]]) -> bool:
    verdicts = (gate_overview or {}).get("verdicts") or {}
    return any((v or {}).get("verdict") == "PASS" for v in verdicts.values())


def report(event_store, prices: Dict[str, Dict[str, float]], policy: Dict[str, Any],
           gate_overview: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.utcnow()
    rev_all = sales.revenue(event_store)
    rev_30 = sales.revenue(event_store, costs.days_ago(30, now))
    cost_all = costs.ai_cost(event_store, prices, datetime(2000, 1, 1))
    cost_90 = costs.ai_cost(event_store, prices, costs.days_ago(90, now))
    cost_30 = costs.ai_cost(event_store, prices, costs.days_ago(30, now))
    monthly_cost = cost_90["usd"] / 3.0
    profit = rev_all["net_usd"] - cost_all["usd"]
    tax = max(0.0, rev_all["net_usd"]) * policy["tax_reserve_pct"]
    runway = monthly_cost * policy["runway_months"]
    moved = transferred_total(event_store)
    free = profit - tax - runway - moved
    ready = trading_ready(gate_overview) if policy["require_pass_strategy"] else True
    topup = round(max(0.0, free) * policy["trading_topup_pct"], 2)
    out: Dict[str, Any] = {
        "revenue_net_usd": rev_all["net_usd"], "revenue_30d_usd": rev_30["net_usd"], "sales": rev_all["sales"],
        "ai_cost_usd": cost_all["usd"], "ai_cost_30d_usd": cost_30["usd"], "ai_cost_estimated_usd": cost_all["estimated_usd"],
        "profit_usd": round(profit, 2), "tax_reserve_usd": round(tax, 2), "runway_reserve_usd": round(runway, 2),
        "transferred_usd": moved, "free_usd": round(free, 2), "trading_ready": ready, "policy": policy,
        "by_product": rev_all["by_product"],
    }
    out["recommendations"] = recommend(out, policy, topup)
    return out


def recommend(r: Dict[str, Any], policy: Dict[str, Any], topup: float) -> List[str]:
    recs: List[str] = []
    if r["sales"] == 0:
        recs.append("Noch keine Verkaeufe: nichts zu verteilen. Kosten klein halten (Ollama), Produkte freigeben.")
        return recs
    if r["profit_usd"] < 0:
        recs.append(f"Die Firma kostet mehr als sie einnimmt ({r['profit_usd']:+.2f} USD). Kein Geld ins Trading; "
                    "Studio-Budget nicht erhoehen.")
    if r["free_usd"] >= policy["min_transfer_usd"]:
        if r["trading_ready"] and topup >= policy["min_transfer_usd"]:
            recs.append(f"Empfehlung: {topup:.2f} USD aufs Trading-Konto ueberweisen (deine Regel: "
                        f"{policy['trading_topup_pct']:.0%} vom freien Ueberschuss). Danach eintragen: "
                        f"firm treasury transferred {topup:.2f}")
        elif not r["trading_ready"]:
            recs.append(f"{r['free_usd']:.2f} USD frei, aber keine Strategie hat die Pruefung bestanden (PASS): "
                        "Geld NICHT ins Trading, als Polster behalten.")
    if r["revenue_30d_usd"] > 0 and r["revenue_30d_usd"] >= 3 * max(r["ai_cost_30d_usd"], 0.01):
        recs.append(f"Einnahmen der letzten 30 Tage ({r['revenue_30d_usd']:.2f} USD) decken die KI-Kosten "
                    f"({r['ai_cost_30d_usd']:.2f} USD) mehr als 3-fach: Du koenntest [studio] max_monthly_ai_usd "
                    "in config/limits.toml massvoll erhoehen (deine Entscheidung).")
    recs.append(f"Steuer-Ruecklage behalten: {r['tax_reserve_usd']:.2f} USD (ANNAHME {policy['tax_reserve_pct']:.0%} - "
                "mit dem Steueramt klaeren).")
    return recs


class TreasuryReporter:
    """Taeglicher Bericht + Meldung, wenn sich die Empfehlung aendert."""

    def __init__(self, event_store, prices, policy, gate_overview_fn: Callable[[], Dict[str, Any]] = lambda: {}):
        self.store, self.prices, self.policy, self.gate = event_store, prices, policy, gate_overview_fn
        self._last_day: Optional[str] = None
        self._last_recs: Optional[List[str]] = None

    def tick(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        now = now or datetime.utcnow()
        r = report(self.store, self.prices, self.policy, self.gate(), now)
        day = now.date().isoformat()
        if day != self._last_day:
            self.store.append("TREASURY", "TREASURY_REPORT", {k: v for k, v in r.items() if k != "by_product"})
            self._last_day = day
        actionable = [x for x in r["recommendations"] if x.startswith(("Empfehlung", "Die Firma kostet", "Einnahmen"))]
        if actionable and actionable != self._last_recs:
            self.store.append("TREASURY", "TREASURY_RECOMMENDATION", {"recommendations": actionable,
                                                                      "free_usd": r["free_usd"]})
        self._last_recs = actionable
        return r
