"""Prueft, ob die Limits in config/limits.toml zueinander passen (liest nur, aendert nie etwas).

Typischer Fehler beim Erhoehen des Risikos je Trade: Tagesverlust-Grenze oder Gesamtrisiko bleiben klein.
Dann haelt schon ein einziger Stop-Treffer die ganze Firma an, oder jeder Trade wird als Klumpenrisiko abgelehnt.
"""
from __future__ import annotations

from typing import Any, Dict, List

DEFAULT_TOTAL_RISK = 0.015


def limit_warnings(limits: Dict[str, Any]) -> List[str]:
    trade, account = limits.get("trade", {}), limits.get("account", {})
    risk = float(trade.get("max_risk_per_trade", 0) or 0)
    total = float(trade.get("max_total_risk", DEFAULT_TOTAL_RISK) or 0)
    daily = float(account.get("daily_loss_limit", 0) or 0)
    dd = float(account.get("max_drawdown_limit", 0) or 0)
    out: List[str] = []
    if risk <= 0:
        return ["max_risk_per_trade fehlt oder ist 0 - es wird nichts gehandelt"]
    if total < risk:
        out.append(f"max_total_risk {total:.1%} < max_risk_per_trade {risk:.1%}: jeder Trade wird als Klumpenrisiko "
                   f"abgelehnt (mind. {risk:.1%}, sinnvoll {2 * risk:.1%})")
    if daily <= risk * 1.1:
        out.append(f"daily_loss_limit {daily:.1%} <= Risiko je Trade {risk:.1%}: schon EIN Stop-Treffer (plus Spread) "
                   f"stellt alles glatt und haelt die Firma an (sinnvoll >= {2 * risk + 0.005:.1%})")
    if dd < 3 * risk:
        out.append(f"max_drawdown_limit {dd:.1%} < 3 x Risiko je Trade: wenige Verluste in Folge halten die Firma an")
    return out
