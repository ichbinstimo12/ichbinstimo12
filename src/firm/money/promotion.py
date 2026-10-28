"""Skalierungs-Leiter: rechnerische Pruefung, ob der naechste Schritt (PAPER -> LIVE_MICRO -> LIVE) begruendet waere.

Die Firma schaltet NIE selbst hoch. Diese Pruefung ist eine Empfehlung mit Begruendung; den Schritt machst nur du
(`python -m src.firm approve-live` + Start mit --mode LIVE_MICRO). Innerhalb einer Stufe waechst die Positionsgroesse
automatisch mit dem Konto (Risiko = fester Prozentsatz der Equity) - das ist das einzige "Hochskalieren", das der
Code selbst tut, und es gilt in beide Richtungen.

Kriterien (Spezifikation Phase 5/6):
  1. mind. 28 Tage Demo-Betrieb mit Handel
  2. mind. 50 abgeschlossene Trades mit gemessenem R
  3. Live-Erwartungswert > 0 und mind. halb so gross wie im Backtest
  4. max. Drawdown der Demo-Equity < halbes Drawdown-Limit
  5. keine Kill-Kriterien / Halts in den letzten 14 Tagen
  6. mit dem LIVE-Kontostand ist mind. ein freigegebener Markt ueber der Mindestgroesse handelbar
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

LADDER = ["PAPER", "LIVE_MICRO", "LIVE"]


def _p(e) -> Dict[str, Any]:
    try:
        return json.loads(e.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}


def promotion_check(event_store, gate, limits: Dict[str, Any], mode: str = "PAPER",
                    live_balance: Optional[float] = None, markets: Optional[Dict[str, dict]] = None,
                    min_days: int = 28, min_trades: int = 50) -> Dict[str, Any]:
    stage = mode if mode in LADDER else "PAPER"
    nxt = LADDER[min(LADDER.index(stage) + 1, len(LADDER) - 1)]
    crit: List[Dict[str, Any]] = []

    fills = event_store.get_events(types=["ORDER_FILLED"])
    first = fills[0]["ts"] if fills else None
    days = (datetime.utcnow() - datetime.fromisoformat(first)).days if first else 0
    crit.append({"name": f"mind. {min_days} Tage Handel in {stage}", "ok": days >= min_days, "detail": f"{days} Tage"})

    results = [_p(e) for e in event_store.get_events(types=["TRADE_RESULT"])]
    rs = [r["r"] for r in results if isinstance(r.get("r"), (int, float))]
    crit.append({"name": f"mind. {min_trades} Trades mit gemessenem R", "ok": len(rs) >= min_trades,
                 "detail": f"{len(rs)} Trades"})

    ov = gate.overview() if gate is not None else {}
    exp_bt = [v.get("oos_expectancy_r") for v in (ov.get("verdicts") or {}).values()
              if v.get("verdict") in ("PASS", "FORWARD_TEST") and isinstance(v.get("oos_expectancy_r"), (int, float))]
    live_e = sum(rs) / len(rs) if rs else None
    bt_e = sum(exp_bt) / len(exp_bt) if exp_bt else None
    ok3 = live_e is not None and live_e > 0 and (bt_e is None or live_e >= 0.5 * bt_e)
    crit.append({"name": "Live-Erwartungswert > 0 und >= 50 % des Backtests", "ok": ok3,
                 "detail": f"live {live_e:.2f} R vs. Backtest {bt_e:.2f} R" if live_e is not None and bt_e is not None
                 else f"live {'—' if live_e is None else f'{live_e:.2f} R'}"})

    eq = [_p(e).get("equity") for e in event_store.get_events(types=["ACCOUNT_SNAPSHOT"])]
    eq = [v for v in eq if isinstance(v, (int, float))]
    peak, dd = (eq[0] if eq else 0.0), 0.0
    for v in eq:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak if peak else 0.0)
    limit = float(limits.get("account", {}).get("max_drawdown_limit", 0.08))
    crit.append({"name": "max. Drawdown < halbes Limit", "ok": bool(eq) and dd < limit / 2,
                 "detail": f"{dd * 100:.1f} % (Limit {limit * 100:.0f} %)"})

    since = (datetime.utcnow() - timedelta(days=14)).isoformat()
    bad = event_store.get_events(after_ts=since, types=["STRATEGY_STOPPED", "TRADING_HALTED"])
    crit.append({"name": "keine Kill-Kriterien/Halts in 14 Tagen", "ok": not bad, "detail": f"{len(bad)} Ereignisse"})

    if live_balance:
        risk_pct = float(limits.get("trade", {}).get("max_risk_per_trade", 0.005)) * (0.5 if nxt == "LIVE_MICRO" else 1.0)
        budget = live_balance * risk_pct
        tradeable, details = [], []
        verdicts = (json.loads(gate.path.read_text()).get("verdicts") or {}) if gate is not None and gate.path.exists() else {}
        for epic, v in verdicts.items():
            if v.get("verdict") not in ("PASS", "FORWARD_TEST"):
                continue
            m, stop_pct = (markets or {}).get(epic), v.get("median_stop_pct")
            if not m or not stop_pct:
                details.append(f"{epic}: unbekannt")
                continue
            risk_min = float(m["min_size"]) * float(stop_pct) * float(m["price"]) * float(m["fx"])
            details.append(f"{epic}: Mindestgroesse riskiert {risk_min:.2f} bei Budget {budget:.2f}")
            if risk_min <= budget:
                tradeable.append(epic)
        crit.append({"name": f"mit {live_balance:g} Live-Kapital handelbar", "ok": bool(tradeable),
                     "detail": "; ".join(details) or "keine freigegebene Strategie"})
    else:
        crit.append({"name": "Live-Kapital bekannt (FIRM_LIVE_ACCOUNT_BALANCE)", "ok": False, "detail": "nicht gesetzt"})

    ready = stage != "LIVE" and all(c["ok"] for c in crit)
    return {"stage": stage, "next": nxt, "ready": ready, "criteria": crit,
            "passed": sum(1 for c in crit if c["ok"]), "total": len(crit),
            "note": "Empfehlung, keine Freigabe. Den Schritt machst nur du (approve-live)."}
