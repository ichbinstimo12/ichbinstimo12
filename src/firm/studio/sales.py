"""Verkaeufe aus dem Shop LESEN (nie schreiben, nie Geld bewegen) und als Ereignisse ablegen.

Gumroad API v2: GET https://api.gumroad.com/v2/sales (Scope view_sales), Token aus GUMROAD_ACCESS_TOKEN, das nur die
Studio-Zone sieht. Antwortformat laut Gumroad-Doku (ANNAHME, beim ersten echten Abruf gepruefte Felder):
  {"success": true, "sales": [{"id", "created_at", "product_id", "product_name", "price" (Cent), "gumroad_fee" (Cent),
   "currency", "refunded"}], "next_page_key": "..."}
Passt eine Antwort nicht zu diesem Format, wird nichts verbucht (STUDIO_ERROR) statt Zahlen zu raten.
Jeder Verkauf wird genau einmal verbucht (Verkaufs-ID); eine spaetere Rueckerstattung als STUDIO_REFUND.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("StudioSales")
API = "https://api.gumroad.com/v2/sales"
MAX_PAGES = 20


def _cents(v: Any) -> Optional[float]:
    try:
        return round(float(v) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def parse_sale(s: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(s, dict) or not s.get("id") or "price" not in s:
        return None
    gross, fee = _cents(s.get("price")), _cents(s.get("gumroad_fee") or 0)
    if gross is None:
        return None
    return {"sale_id": str(s["id"]), "product_ref": str(s.get("product_id") or ""),
            "product_name": str(s.get("product_name") or "")[:120], "currency": str(s.get("currency") or "usd").lower(),
            "gross": gross, "fee": fee or 0.0, "net": round(gross - (fee or 0.0), 2),
            "refunded": bool(s.get("refunded")), "sold_at": s.get("created_at")}


class SalesSync:
    def __init__(self, event_store, http=None, token: Optional[str] = None):
        self.store = event_store
        self.token = token if token is not None else os.getenv("GUMROAD_ACCESS_TOKEN", "")
        self.http = http

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _known(self) -> Tuple[Set[str], Set[str]]:
        sold, refunded = set(), set()
        for e in self.store.get_events(types=["STUDIO_SALE", "STUDIO_REFUND"]):
            sid = json.loads(e.get("payload_json") or "{}").get("sale_id")
            (sold if e["type"] == "STUDIO_SALE" else refunded).add(sid)
        return sold, refunded

    async def fetch(self, after: str) -> List[Dict[str, Any]]:
        rows, page_key = [], None
        for _ in range(MAX_PAGES):
            params = {"after": after, **({"page_key": page_key} if page_key else {})}
            r = await self.http.get(API, params=params, headers={"Authorization": f"Bearer {self.token}"}, timeout=30)
            r.raise_for_status()
            body = r.json()
            if not isinstance(body, dict) or body.get("success") is not True or not isinstance(body.get("sales"), list):
                raise ValueError("unerwartetes Antwortformat der Shop-API")
            rows += body["sales"]
            page_key = body.get("next_page_key")
            if not page_key:
                break
        return rows

    async def sync(self, lookback_days: int = 40) -> Dict[str, int]:
        if not self.configured:
            return {"new": 0, "refunds": 0, "skipped": 0}
        after = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        try:
            raw = await self.fetch(after)
        except Exception as e:          # Fehlermeldung ohne Token (steht nur im Header)
            self.store.append("STUDIO", "STUDIO_ERROR", {"step": "sales_sync", "error": f"{type(e).__name__}: {str(e)[:200]}"})
            return {"new": 0, "refunds": 0, "skipped": 0, "error": 1}
        sold, refunded = self._known()
        new = refunds = skipped = 0
        for s in raw:
            sale = parse_sale(s)
            if sale is None:
                skipped += 1
                continue
            usd = sale["currency"] == "usd"
            payload = {**sale, "gross_usd": sale["gross"] if usd else None, "fee_usd": sale["fee"] if usd else None,
                       "net_usd": sale["net"] if usd else None}
            if sale["sale_id"] not in sold:
                self.store.append("STUDIO", "STUDIO_SALE", payload, correlation_id=sale["product_ref"])
                sold.add(sale["sale_id"])
                new += 1
            if sale["refunded"] and sale["sale_id"] not in refunded:
                self.store.append("STUDIO", "STUDIO_REFUND", payload, correlation_id=sale["product_ref"])
                refunded.add(sale["sale_id"])
                refunds += 1
        return {"new": new, "refunds": refunds, "skipped": skipped}


def revenue(event_store, since: Optional[datetime] = None) -> Dict[str, Any]:
    """Netto-Umsatz in USD (nach Shop-Gebuehr, abzueglich Rueckerstattungen) und je Produkt."""
    after = since.isoformat() if since else None
    net, gross, count, refunds = 0.0, 0.0, 0, 0.0
    by_product: Dict[str, Dict[str, float]] = {}
    for e in event_store.get_events(after_ts=after, types=["STUDIO_SALE", "STUDIO_REFUND"]):
        p = json.loads(e.get("payload_json") or "{}")
        if not isinstance(p.get("net_usd"), (int, float)):
            continue
        ref = p.get("product_ref") or "?"
        row = by_product.setdefault(ref, {"sales": 0, "net_usd": 0.0, "name": p.get("product_name")})
        if e["type"] == "STUDIO_SALE":
            net += p["net_usd"]
            gross += p["gross_usd"] or 0.0
            count += 1
            row["sales"] += 1
            row["net_usd"] += p["net_usd"]
        else:
            refunds += p["net_usd"]
            net -= p["net_usd"]
            row["net_usd"] -= p["net_usd"]
    return {"net_usd": round(net, 2), "gross_usd": round(gross, 2), "sales": count, "refunds_usd": round(refunds, 2),
            "by_product": by_product}
