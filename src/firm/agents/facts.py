"""Faktenblatt fuer die Agenten.

Die Agenten bekommen keinen Broker-Zugang. Sie bekommen dieses Faktenblatt, das ausschliesslich aus dem
Event-Log und den Dateien der Money-Zone gebaut wird (Kontostand-Snapshots, Desk-Status, Strategie-Urteile,
Trade-Ergebnisse, aktive Risiko-Overlays). Dieselben Fakten dienen danach zur automatischen Pruefung der
Antworten: Wer eine Zahl behauptet, die hier anders steht, wird verworfen (AGENT_INVALID).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _payload(e: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(e.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}


def _age_minutes(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return round((datetime.now(timezone.utc) - t).total_seconds() / 60, 1)


def build_facts(event_store, mode: str, gate=None, overlay=None, desk=None) -> Dict[str, Any]:
    facts: Dict[str, Any] = {"mode": mode, "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    snaps = event_store.get_events(types=["ACCOUNT_SNAPSHOT"], last=1)
    if snaps:
        p = _payload(snaps[-1])
        positions = p.get("positions") or []
        facts["account"] = {
            "balance": p.get("balance"), "equity": p.get("equity"), "currency": p.get("currency"),
            "source": p.get("source"), "age_minutes": _age_minutes(snaps[-1].get("ts")),
        }
        facts["open_positions"] = len(positions)
        facts["positions"] = [
            {k: pos.get(k) for k in ("epic", "direction", "volume", "level", "upl", "stop_level", "profit_level")}
            for pos in positions
        ]
    else:
        facts["account"] = "UNKNOWN (kein Snapshot)"

    status = event_store.get_events(types=["DESK_STATUS"], last=1)
    if status:
        p = _payload(status[-1])
        facts["desk"] = {"since_utc": status[-1].get("ts"), "error": p.get("error"), "why_no_entry": p.get("reasons")}

    if gate is not None:
        ov = gate.overview()
        facts["research"] = {
            "verdicts_created_utc": datetime.fromtimestamp(ov["created"], timezone.utc).isoformat(timespec="minutes")
            if ov.get("created") else None,
            "trial_counter": ov.get("trials"),
            "verdicts": ov.get("verdicts"),
        }

    results = event_store.get_events(types=["TRADE_RESULT"], last=20)
    rows = [_payload(e) | {"ts": e.get("ts")} for e in results]
    facts["recent_trade_results"] = [
        {k: r.get(k) for k in ("ts", "epic", "side", "strategy", "pnl", "r", "lane", "exact")} for r in rows[-8:]
    ]
    rs = [r["r"] for r in rows if isinstance(r.get("r"), (int, float))]
    facts["live_stats"] = {"closed_trades": len(rows), "trades_with_r": len(rs),
                           "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
                           "note": "unter ~100 Trades ist keine Aussage ueber den Edge moeglich"}

    fills = event_store.get_events(types=["ORDER_FILLED"], last=1)
    facts["last_fill_utc"] = fills[-1].get("ts") if fills else None

    alerts = event_store.get_events(types=["STRATEGY_STOPPED", "TRADING_HALTED", "AGENT_INVALID", "RESEARCH_FAILED"], last=5)
    facts["recent_alerts"] = [{"ts": e.get("ts"), "type": e.get("type"), **{k: v for k, v in _payload(e).items()
                                                                           if k in ("epic", "reason", "reasons", "agent", "error")}}
                              for e in alerts]

    uni = event_store.get_events(types=["UNIVERSE_UPDATED"], last=1)
    if uni:
        p = _payload(uni[-1])
        facts["scanner"] = {
            "universe_updated_utc": uni[-1].get("ts"), "markets_total": p.get("markets_total"),
            "top_candidates": [{k: c.get(k) for k in ("epic", "spread_pct", "change_pct")}
                               for c in (p.get("candidates") or [])[:8]],
            "recent_hits": [{k: v for k, v in _payload(e).items() if k in ("epic", "strategy", "side", "whitelisted")}
                            | {"ts": e.get("ts")} for e in event_store.get_events(types=["SCANNER_HIT"], last=8)],
        }
    setups: Dict[str, Any] = {}
    for e in event_store.get_events(types=["SMC_SETUP"], last=40):
        p = _payload(e)
        if p.get("epic"):
            setups[p["epic"]] = {"stage": p.get("stage"), "side": p.get("side"), "whitelisted": p.get("whitelisted"),
                                 "levels": p.get("levels"), "ts": e.get("ts")}
    if setups:
        facts["smc_setups"] = dict(list(setups.items())[-10:])

    if overlay is not None:
        facts["risk_overlays"] = [
            {"epic": r["epic"], "factor": r["factor"], "agent": r["agent_id"], "reason": r["reason"],
             "expires_utc": datetime.fromtimestamp(r["expires_at"], timezone.utc).isoformat(timespec="minutes")}
            for r in overlay.active()
        ]
    return facts


# -------------------------------------------------------------- fact checking
def check_claims(claims: Any, facts: Dict[str, Any]) -> List[str]:
    """Vergleicht behauptete Zahlen mit dem Faktenblatt. Rueckgabe: Liste der Widersprueche (leer = ok)."""
    problems: List[str] = []
    if not isinstance(claims, dict):
        return problems
    acct = facts.get("account") if isinstance(facts.get("account"), dict) else {}
    known = {
        "open_positions": facts.get("open_positions"),
        "balance": acct.get("balance"),
        "equity": acct.get("equity"),
        "closed_trades": (facts.get("live_stats") or {}).get("closed_trades"),
    }
    for key, claimed in claims.items():
        if key not in known:
            continue
        truth = known[key]
        if truth is None:
            problems.append(f"{key}: behauptet {claimed}, Fakt ist UNBEKANNT")
            continue
        try:
            c, t = float(claimed), float(truth)
        except (TypeError, ValueError):
            problems.append(f"{key}: '{claimed}' ist keine Zahl")
            continue
        tol = 0.0 if key in ("open_positions", "closed_trades") else max(0.01, abs(t) * 0.002)
        if abs(c - t) > tol:
            problems.append(f"{key}: behauptet {c:g}, Fakt ist {t:g}")
    return problems
