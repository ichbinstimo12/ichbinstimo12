"""Executor (PAPER) und Exit-Logik des Memecoin-Desks.

PAPER: Kauf/Verkauf werden NICHT gesendet, sondern mit einer echten Jupiter-Quote simuliert (echter Preis-Impact der
Route), dazu dynamische Slippage, Swap-Gebuehr und Priority-Fee je Transaktion. So sind PAPER-Ergebnisse eher zu
schlecht als zu gut.
LIVE: absichtlich nicht gebaut (NotImplementedError). Laut Spezifikation erst nach >= 4 Wochen PAPER mit >= 100 Trades
und positivem Ergebnis; dann mit verschluesseltem Schluessel nur im Executor-Prozess und App-Bestaetigung je Kauf.

Exits (Orangie-Stil), in dieser Reihenfolge geprueft:
  1. Sofort raus: Narrativ-Bruch, Dev-Verkauf, Smart-Money-Exit, Safety-Problem
  2. harter Stop (-30 %)
  3. Free Ride: bei +100 % den Einsatz zurueckholen
  4. Stufen-Verkaeufe auf dem Rest
  5. Trailing-Stop auf den Rest (vom Hoch)
  6. Zeit-Stop: nach X Minuten ohne Bewegung (+/- Band) schliessen
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from src.firm.memedesk.sources import LAMPORTS, SOL_MINT


def dynamic_slippage_bps(impact_pct: float, cfg: Dict[str, Any]) -> int:
    ex = cfg["execution"]
    return int(min(ex["slippage_bps_max"], max(ex["slippage_bps_min"], impact_pct * 100 * 2 + 100)))


def paper_fill(quote: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[float, int]:
    """Konservativer Fill: Quote-Ergebnis minus halbe Slippage-Toleranz -> (Faktor auf `out`, slippage_bps)."""
    bps = dynamic_slippage_bps(float(quote.get("impact_pct") or 0), cfg)
    return 1 - bps / 2 / 10_000, bps


async def paper_buy(sources, mint: str, sol: float, cfg: Dict[str, Any]) -> Dict[str, Any]:
    q = await sources.quote(SOL_MINT, mint, int(sol * LAMPORTS))
    factor, bps = paper_fill(q, cfg)
    fee = cfg["execution"]["priority_fee_sol"]
    tokens = q["out"] * factor
    if tokens <= 0:
        raise RuntimeError("Jupiter liefert keine Route fuer den Kauf")
    return {"tokens_raw": tokens, "cost_sol": sol + fee, "slippage_bps": bps, "impact_pct": q["impact_pct"],
            "route": q["route"], "fee_sol": fee}


async def paper_sell_value(sources, mint: str, tokens_raw: float, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Was ein Verkauf JETZT bringen wuerde (Quote, Slippage, Gebuehr) - dient auch der Bewertung offener Positionen."""
    if tokens_raw <= 0:
        return {"sol": 0.0, "slippage_bps": 0, "impact_pct": 0.0, "route": []}
    q = await sources.quote(mint, SOL_MINT, int(tokens_raw))
    factor, bps = paper_fill(q, cfg)
    sol = max(0.0, q["out"] / LAMPORTS * factor - cfg["execution"]["priority_fee_sol"])
    return {"sol": sol, "slippage_bps": bps, "impact_pct": q["impact_pct"], "route": q["route"]}


def estimate_value(pos: Dict[str, Any], prices: Dict[str, Dict[str, float]], cfg: Dict[str, Any]) -> Optional[float]:
    """Bewertung ohne Quote: Token-Menge x Preis (Jupiter-Preis-API), umgerechnet in SOL, minus geschaetzte
    Ausstiegskosten (Slippage/Impact vom Einstieg, Swap-Gebuehr, Priority-Fee). None = kein Preis."""
    p, s = prices.get(pos["mint"]), prices.get(SOL_MINT)
    if not p or not s or not s.get("usd") or p.get("decimals") is None:
        return None
    tokens = float(pos["tokens_raw"]) / (10 ** int(p["decimals"]))
    gross = tokens * p["usd"] / s["usd"]
    cost = float(pos.get("exit_cost_pct") or 3.0) / 100 + cfg["execution"]["dex_fee_pct"] / 100
    return max(0.0, gross * (1 - cost) - cfg["execution"]["priority_fee_sol"])


class LiveExecutor:
    """Bewusst nicht implementiert - siehe Moduldoku und docs/memedesk.md (Freischaltung)."""

    def __init__(self, *_, **__):
        raise NotImplementedError("LIVE-Ausfuehrung ist noch nicht gebaut: erst nach erfuellter PAPER-Pruefung "
                                  "(>= 4 Wochen, >= 100 Trades, positiv) - siehe docs/memedesk.md")


# ------------------------------------------------------------------ exit decisions (pure)
def exit_decision(pos: Dict[str, Any], value_sol: float, now: float, cfg: Dict[str, Any],
                  limits: Dict[str, Any], alerts: Optional[List[str]] = None) -> Optional[Tuple[str, float]]:
    """-> (Grund, Anteil der aktuellen Position, der verkauft wird) oder None.

    pos: cost_sol (Einsatz), tokens_raw (aktuell), peak_value, opened_at, free_ride (bool), tiers_done (list),
         realized_sol, last_move_at."""
    ex = cfg["exits"]
    if alerts:
        return "sofort raus: " + "; ".join(alerts), 1.0
    cost = float(pos["cost_sol"])
    # P&L der Restposition inkl. bereits realisierter Teilverkaeufe, gemessen am Einsatz
    total = value_sol + float(pos.get("realized_sol") or 0)
    gain_pct = (total / cost - 1) * 100 if cost else 0.0
    if not pos.get("free_ride") and value_sol <= cost * (1 - limits["trade"]["hard_stop_pct"]):
        return f"harter Stop -{limits['trade']['hard_stop_pct']:.0%}", 1.0
    if not pos.get("free_ride") and gain_pct >= ex["free_ride_at_pct"]:
        frac = min(1.0, cost / value_sol) if value_sol > 0 else 1.0
        return f"Free Ride bei +{gain_pct:.0f} % (Einsatz zurueck)", frac
    for level, share in ex["tiers"]:
        if pos.get("free_ride") and gain_pct >= level and level not in (pos.get("tiers_done") or []):
            return f"Stufe +{level:.0f} %: {share:.0%} verkaufen", float(share)
    peak = max(float(pos.get("peak_value") or 0), value_sol)
    if pos.get("free_ride") and peak > 0 and value_sol <= peak * (1 - ex["trailing_pct"] / 100):
        return f"Trailing-Stop -{ex['trailing_pct']:.0f} % vom Hoch", 1.0
    if not pos.get("free_ride") and now - float(pos.get("last_move_at") or pos["opened_at"]) >= ex["time_stop_minutes"] * 60 \
            and abs(gain_pct) < ex["time_stop_band_pct"]:
        return f"Zeit-Stop: {ex['time_stop_minutes']} min ohne Bewegung", 1.0
    return None


def apply_exit(pos: Dict[str, Any], reason: str, frac: float, sold_sol: float) -> bool:
    """Position nach (Teil-)Verkauf fortschreiben. -> True, wenn die Position geschlossen ist."""
    pos["tokens_raw"] = float(pos["tokens_raw"]) * (1 - frac)
    pos["realized_sol"] = float(pos.get("realized_sol") or 0) + sold_sol
    if reason.startswith("Free Ride"):
        pos["free_ride"] = True
    elif reason.startswith("Stufe"):
        level = float(reason.split("+")[1].split(" ")[0])
        pos.setdefault("tiers_done", []).append(level)
    return frac >= 0.999 or pos["tokens_raw"] <= 0


def mark(pos: Dict[str, Any], value_sol: float, now: float, band_pct: float) -> None:
    """Hoch und letzte deutliche Bewegung fuer Trailing- und Zeit-Stop merken."""
    pos["peak_value"] = max(float(pos.get("peak_value") or 0), value_sol)
    ref = float(pos.get("last_move_value") or pos["cost_sol"])
    if ref and abs(value_sol / ref - 1) * 100 >= band_pct:
        pos["last_move_at"], pos["last_move_value"] = now, value_sol


def now() -> float:
    return time.time()
