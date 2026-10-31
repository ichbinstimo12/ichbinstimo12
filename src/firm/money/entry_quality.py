"""Letzte Pruefung vor dem Senden einer Order (Money-Zone, deterministisch, kein LLM).

Entspricht "simulate before send / min_amount_out" bei On-Chain-Bots: Mit dem echten Live-Kurs wird nachgerechnet,
ob der Trade nach Kosten noch der Trade ist, den die Strategie wollte:
  * Spread in R: frisst der Spread zu viel vom Risiko, ist der Vorteil weg (enge Stops auf kleinen Kerzen)
  * Kursdrift in R: hat sich der Kurs seit dem Signal gegen uns bewegt, wird nicht hinterhergerannt
Bewusst KEINE feste Chance/Risiko-Schwelle: ob sich eine Strategie lohnt, entscheidet ihr gemessener Erwartungswert
(Trefferquote x Gewinn), nicht ein Mindest-Verhaeltnis - eine 1:1-Strategie mit 60 % Treffern ist profitabel.
Fehlt ein Kurs oder sind die Marken unsinnig -> ablehnen (fail closed).

Beide Schwellen koennen per Umgebungsvariable nur VERSCHAERFT werden.
"""
from __future__ import annotations

import os
from typing import Optional

MAX_COST_R = 0.20       # Spread hoechstens 20 % des Risikos
MAX_DRIFT_R = 0.25      # Einstieg hoechstens 0.25 R schlechter als das Signal
THIN_EDGE_R = 0.15      # out-of-sample-Erwartung darunter: halbes Risiko
NO_EDGE_R = 0.0         # darunter (sollte nie PASS sein): Viertel-Risiko


def _tightened(name: str, default: float, tighter_is_lower: bool) -> float:
    try:
        v = float(os.getenv(name, default))
    except ValueError:
        return default
    return min(v, default) if tighter_is_lower else max(v, default)


def entry_check(side: str, entry: float, stop: float, target: Optional[float],
                bid: Optional[float], ask: Optional[float]) -> Optional[str]:
    """-> Grund fuer die Ablehnung, oder None (Order darf raus)."""
    max_cost = _tightened("FIRM_MAX_COST_R", MAX_COST_R, True)
    max_drift = _tightened("FIRM_MAX_DRIFT_R", MAX_DRIFT_R, True)
    if not bid or not ask or ask < bid:
        return "kein gueltiger Live-Kurs (Bid/Ask) vor dem Senden"
    d = 1 if str(side).upper() == "BUY" else -1
    risk = (entry - stop) * d
    if risk <= 0:
        return "Stop liegt nicht auf der Verlustseite des Einstiegs"
    cost_r = (ask - bid) / risk
    if cost_r > max_cost:
        return f"Spread {cost_r:.2f} R > {max_cost:.2f} R - Kosten fressen den Vorteil"
    executable = ask if d > 0 else bid
    drift_r = (executable - entry) * d / risk
    if drift_r > max_drift:
        return f"Kurs ist {drift_r:.2f} R vom Signal weggelaufen (> {max_drift:.2f} R) - nicht hinterherrennen"
    if target is not None and (target - executable) * d <= 0:
        return "Ziel ist vom aktuellen Kurs aus schon erreicht - kein Einstieg mehr"
    return None


def backtest_entry_ok(stop_dist: float, target_dist: Optional[float], spread: float) -> bool:
    """Dieselbe Kostenregel im Backtest (ohne Drift: dort ist der Einstieg der naechste Open).
    So zaehlen Forschung und Livehandel dieselben Trades."""
    if stop_dist <= 0:
        return False
    return max(0.0, spread) / stop_dist <= MAX_COST_R


def edge_factor(oos_expectancy_r: Optional[float]) -> float:
    """Risiko-Faktor nach Staerke des belegten Vorteils (Bruchteil-Kelly-Gedanke). Nur <= 1: erhoeht nie das Risiko
    ueber die Limits; ein knapper Vorteil bekommt weniger Geld, ein unbekannter bleibt beim Faktor 1 des Gates."""
    if oos_expectancy_r is None:
        return 1.0
    if oos_expectancy_r < NO_EDGE_R:
        return 0.25
    if oos_expectancy_r < THIN_EDGE_R:
        return 0.5
    return 1.0
