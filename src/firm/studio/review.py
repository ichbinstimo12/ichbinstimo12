"""Produkt-Ueberpruefung: was im Shop liegt und sich nicht verkauft, wird dir zur Entscheidung gemeldet.

Kill-Kriterium (wie bei Strategien, vorab festgelegt): veroeffentlicht seit STALE_DAYS Tagen und kein einziger
Verkauf -> STUDIO_REVIEW (einmal je Produkt) mit Vorschlag. Zurueckziehen tust du (Dashboard/CLI); die Nischenwahl
des Studios zaehlt das Produkt ohnehin schon als "ohne Umsatz".
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import List, Optional

STALE_DAYS = 45


def review_products(catalog, event_store, now: Optional[datetime] = None, days: int = STALE_DAYS) -> List[str]:
    now = now or datetime.utcnow()
    sold = set()
    for e in event_store.get_events(types=["STUDIO_SALE"]):
        prod = catalog.match_sale(json.loads(e.get("payload_json") or "{}"))
        if prod:
            sold.add(prod["id"])
    flagged = {json.loads(e.get("payload_json") or "{}").get("product_id")
               for e in event_store.get_events(types=["STUDIO_REVIEW"])}
    out = []
    for p in catalog.list(["PUBLISHED"], limit=10_000):
        if p["id"] in sold or p["id"] in flagged:
            continue
        if datetime.fromisoformat(p["updated_at"]) <= now - timedelta(days=days):
            event_store.append("STUDIO", "STUDIO_REVIEW", {
                "product_id": p["id"], "title": p["title"], "days_published": days,
                "suggestion": "kein Verkauf seit Veroeffentlichung: Marketing-Posts genutzt? Sonst zurueckziehen "
                              "(firm studio retire) - das Studio setzt dann auf Nischen, die sich verkaufen."},
                correlation_id=p["id"])
            out.append(p["id"])
    return out
