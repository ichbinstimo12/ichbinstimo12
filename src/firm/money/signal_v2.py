"""Live-Signal-Engine v2: nutzt dieselben Strategien wie das Forschungslabor (ein Code, keine Backtest/Live-Abweichung).

Behebt gegenueber v1: (1) rechnet nur auf ABGESCHLOSSENEN Kerzen (v1 nahm die laufende mit), (2) pro Kerze genau eine
Auswertung (v1: alle 180 s neu), (3) Einstieg nur bei NEUEM Signal (Kanten-Trigger), nicht im Dauerzustand,
(4) Spread-Filter, (5) kein festes enges Ziel: Trendstrategien laufen mit Exit-Regel.
Gleiche Schnittstelle wie SignalEngine: evaluate(epic, candles, bid, ask) -> Signal | None.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.firm.research import lab


@dataclass
class Signal:
    epic: str
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    atr: float
    fast: float
    slow: float
    strength: float
    reason: str


class SignalEngineV2:
    def __init__(self, strategy: str = "donchian_trend", params: Optional[dict] = None, resolution: str = "HOUR",
                 max_spread_atr: float = 0.10, far_target_atr: float = 10.0, now_fn=time.time):
        self.strategy_name = strategy
        self.strat = lab.REGISTRY[strategy](**(params or {}))
        self.resolution_s = lab.RES_SECONDS[resolution]
        self.max_spread_atr, self.far_target_atr, self.now = max_spread_atr, far_target_atr, now_fn
        self._last_bar: Dict[str, float] = {}

    @property
    def warmup(self) -> int:
        return self.strat.warmup() + 5

    def _closed(self, candles: List[Dict[str, Any]]) -> list:
        cs = lab.normalize(candles)
        now = self.now()
        return [c for c in cs if c["ts"] + self.resolution_s <= now + 1]

    def evaluate(self, epic, candles, bid=None, ask=None) -> Optional[Signal]:
        cs = self._closed(candles)
        if len(cs) < self.warmup:
            return None
        i = len(cs) - 1
        bar_ts = cs[i]["ts"]
        if self._last_bar.get(epic) == bar_ts:
            return None                      # diese Kerze wurde schon ausgewertet
        self._last_bar[epic] = bar_ts
        if not self.strat.can_enter_at(self.now()):
            return None                      # Daytrading: ausserhalb des Handelsfensters keine Einstiege
        ind = lab.Ind(cs)
        sig = self.strat.entry(ind, i)
        if not sig:
            return None
        if self.strat.entry(ind, i - 1):     # Kanten-Trigger: Signal darf nicht schon eine Kerze vorher bestanden haben
            return None
        side, sd, tp = sig
        a = ind.atr(14)[i]
        if bid and ask and a > 0 and (ask - bid) > self.max_spread_atr * a:
            return None                      # Spread frisst zu viel der Bewegung
        d = 1 if side == "BUY" else -1
        entry = float(ask if side == "BUY" else bid) if (bid and ask) else cs[i]["c"]
        stop = entry - d * sd
        target = entry + d * (tp if tp else self.far_target_atr * a)
        if stop <= 0 or target <= 0:
            return None
        return Signal(epic=epic, side=side, entry=entry, stop_loss=stop, take_profit=target, atr=a,
                      fast=ind.ema(12)[i], slow=ind.ema(200)[i], strength=abs(cs[i]["c"] - ind.ema(200)[i]) / a if a else 0.0,
                      reason=f"{self.strategy_name} {side}: Schluss {cs[i]['c']:.5g}, ATR {a:.5g}, Stop {sd:.5g} ({sd / a:.1f} ATR), "
                             f"Kerze {int(bar_ts)}")

    def should_exit(self, epic, candles, side: str, opened_ts: Optional[float] = None) -> bool:
        """Exit-Regel der Strategie auf der letzten abgeschlossenen Kerze (Gegenkanal/Signalwechsel/Zeit-Stop).
        Der Zeit-Stop (`max_bars`) gilt wie im Backtest: Kerzen seit Einstieg >= max_bars -> raus."""
        if opened_ts and self.strat.must_flat(opened_ts, self.now()):
            return True                      # Daytrading: vor Sitzungsende bzw. am Tageswechsel glattstellen
        cs = self._closed(candles)
        if len(cs) < self.warmup:
            return False
        if opened_ts and self.strat.max_bars:
            held = sum(1 for c in cs if c["ts"] >= opened_ts)
            if held >= self.strat.max_bars:
                return True
        return bool(self.strat.exit(lab.Ind(cs), len(cs) - 1, side))
