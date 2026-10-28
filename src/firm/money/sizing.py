"""Positionsgroesse mit Waehrungsumrechnung. Bug in v1: Risiko = Stop-Abstand x Groesse wurde in der QUOTE-Waehrung
(z. B. USD) gerechnet, das Budget aber in Kontowaehrung (CHF). In deinen Trades lag das echte Risiko dadurch
konstant bei ~0.83 R (Verluste -0.82/-0.83 R, Gewinn +1.66 R statt +2 R)."""
from __future__ import annotations

import math
from typing import Dict, Tuple


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(math.floor((value + 1e-12) / step) * step, 10)


def size_position_v2(entry: float, stop: float, equity: float, risk_pct: float, fx_quote_to_account: float,
                     market: Dict, risk_factor: float = 1.0, max_leverage: float = 10.0) -> Tuple[float, str, float]:
    """Rueckgabe: (size, begruendung, echtes_risiko_in_kontowaehrung). size 0 = nicht handeln."""
    dist = abs(entry - stop)
    if equity <= 0 or dist <= 0 or fx_quote_to_account <= 0:
        return 0.0, "ungueltige Eingaben (Konto/Stop/Wechselkurs)", 0.0
    budget = equity * risk_pct * risk_factor
    step = float(market.get("size_step") or 0.01)
    mn = float(market.get("min_size") or step)
    mx = float(market.get("max_size") or 0.0)
    raw = budget / (dist * fx_quote_to_account)
    if mx > 0:
        raw = min(raw, mx)
    size = floor_to_step(raw, step)
    cap = floor_to_step(equity * max_leverage / (entry * fx_quote_to_account), step)
    size = min(size, cap)
    if size < mn:
        need = mn * dist * fx_quote_to_account
        return 0.0, f"Mindestgroesse {mn} wuerde {need:.2f} riskieren (Budget {budget:.2f})", 0.0
    risk = size * dist * fx_quote_to_account
    return size, f"Risiko {risk:.2f} von Budget {budget:.2f} (Stop {dist:.6g} x {size} x FX {fx_quote_to_account:.4f})", risk
