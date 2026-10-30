"""Wo verdient, wo verliert die Firma Geld? - ausschliesslich aus echten Ereignissen berechnet.

Quellen: TRADE_RESULT (Capital.com-Desk, P&L aus Kontostand), MEME_TRADE_CLOSE (Memecoin-Desk, PAPER/LIVE),
SHADOW_RESULT (nicht gehandelte Scanner-Signale, Spread abgezogen) und TRADE_REJECTED (was das RiskGate abgelehnt hat).
Befunde werden erst ab MIN_N Trades je Gruppe formuliert - darunter steht nur die Zahl. Keine Prognose.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from src.firm.research import shadow_stats

MIN_N = 5


def _p(e: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(e.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}


def _group(rows: Iterable[Dict[str, Any]], key, pnl_key: str, r_key: Optional[str] = None) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        if isinstance(r.get(pnl_key), (int, float)):
            groups.setdefault(str(key(r)), []).append(r)
    out = []
    for k, rs in groups.items():
        pnls = [float(r[pnl_key]) for r in rs]
        rr = [float(r[r_key]) for r in rs if r_key and isinstance(r.get(r_key), (int, float))]
        out.append({"key": k, "n": len(rs), "pnl": round(sum(pnls), 6),
                    "avg_pnl": round(sum(pnls) / len(pnls), 6),
                    "win_rate": round(sum(1 for x in pnls if x > 0) / len(pnls), 3),
                    "expectancy_r": round(sum(rr) / len(rr), 3) if rr else None})
    return sorted(out, key=lambda g: g["pnl"])


def _combo(r: Dict[str, Any]) -> str:
    on = sorted(k for k, v in (r.get("signals") or {}).items() if v)
    return "+".join(on) or "keine"


def _exit_kind(r: Dict[str, Any]) -> str:
    reason = str(r.get("reason") or "?")
    return reason.split(":")[0].split(" bei ")[0].split(" -")[0].strip()[:40]


def build(event_store, days: float = 30.0) -> Dict[str, Any]:
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    ev = lambda types: event_store.get_events(types=types, after_ts=since)
    trades = [_p(e) for e in ev(["TRADE_RESULT"])]
    meme = [_p(e) for e in ev(["MEME_TRADE_CLOSE"])]
    shadow = shadow_stats.summarize(ev(["SHADOW_RESULT"]))
    solid_shadow = [g for g in shadow if g["n"] >= MIN_N]
    rejected = Counter(str(_p(e).get("reason") or "?")[:80] for e in ev(["TRADE_REJECTED"]))
    report = {
        "days": days,
        "capital": {
            "trades": len(trades),
            "pnl": round(sum(float(t["pnl"]) for t in trades if isinstance(t.get("pnl"), (int, float))), 2) if trades else None,
            "by_strategy": _group(trades, lambda t: t.get("strategy") or "?", "pnl", "r"),
            "by_market": _group(trades, lambda t: t.get("epic") or "?", "pnl", "r"),
            "by_lane": _group(trades, lambda t: t.get("lane") or "?", "pnl", "r"),
        },
        "memecoin": {
            "trades": len(meme),
            "pnl_sol": round(sum(float(m["pnl_sol"]) for m in meme if isinstance(m.get("pnl_sol"), (int, float))), 6)
            if meme else None,
            "by_signals": _group(meme, _combo, "pnl_sol"),
            "by_exit": _group(meme, _exit_kind, "pnl_sol"),
        },
        "shadow": {"groups": len(shadow),
                   "best": sorted(solid_shadow, key=lambda g: -g["expectancy_r"])[:8],
                   "worst": sorted(solid_shadow, key=lambda g: g["expectancy_r"])[:5],
                   "research_candidates": shadow_stats.promising(shadow)},
        "rejections": dict(rejected.most_common(8)),
    }
    report["findings"] = findings(report)
    return report


_CACHE: Dict[str, Any] = {}


def cached_findings(event_store, ttl_s: float = 300.0, now_fn=time.monotonic) -> List[str]:
    """Kurzbefunde fuer die Agenten-Fakten; hoechstens alle ttl_s neu berechnet (jeder Agenten-Schritt fragt)."""
    key = str(getattr(event_store, "db_path", id(event_store)))
    hit = _CACHE.get(key)
    if hit and now_fn() - hit[0] < ttl_s:
        return hit[1]
    res = build(event_store, days=30)["findings"]
    _CACHE[key] = (now_fn(), res)
    return res


def findings(r: Dict[str, Any]) -> List[str]:
    """Deterministische Kurzbefunde - nur fuer Gruppen mit genug Trades."""
    out: List[str] = []
    for label, groups, unit in (("Strategie", r["capital"]["by_strategy"], ""),
                                ("Markt", r["capital"]["by_market"], ""),
                                ("Memecoin-Signale", r["memecoin"]["by_signals"], " SOL"),
                                ("Memecoin-Ausstieg", r["memecoin"]["by_exit"], " SOL")):
        solid = [g for g in groups if g["n"] >= MIN_N]
        if not solid:
            continue
        worst, best = solid[0], solid[-1]
        if worst["pnl"] < 0:
            out.append(f"Groesster Verlustbringer ({label}): {worst['key']} mit {worst['pnl']:+.4g}{unit} "
                       f"aus {worst['n']} Trades (Trefferquote {worst['win_rate']:.0%})")
        if best["pnl"] > 0 and best is not worst:
            out.append(f"Bester Beitrag ({label}): {best['key']} mit {best['pnl']:+.4g}{unit} aus {best['n']} Trades")
    for g in r["shadow"]["research_candidates"][:3]:
        out.append(f"Schattenbuch: {g['epic']} {g['strategy']} {g['resolution']} {g['expectancy_r']:+.2f} R nach Spread "
                   f"ueber {g['n']} Signale - Forschungslauf wird ausgeloest (kein Handel ohne Freigabe)")
    if not out:
        out.append(f"Noch zu wenige abgeschlossene Trades je Gruppe (mind. {MIN_N}) fuer belastbare Befunde.")
    return out


def to_markdown(r: Dict[str, Any]) -> str:
    def table(groups: List[Dict[str, Any]], unit: str) -> List[str]:
        if not groups:
            return ["- keine Daten"]
        rows = ["| Gruppe | Trades | P&L | Ø je Trade | Treffer | Ø R |", "|---|---|---|---|---|---|"]
        for g in sorted(groups, key=lambda x: -x["pnl"]):
            er = f"{g['expectancy_r']:+.2f}" if g["expectancy_r"] is not None else "—"
            rows.append(f"| {g['key']} | {g['n']} | {g['pnl']:+.4g}{unit} | {g['avg_pnl']:+.4g}{unit} | "
                        f"{g['win_rate']:.0%} | {er} |")
        return rows

    lines = [f"## Wo verdient / verliert die Firma Geld? (letzte {r['days']:g} Tage)", ""]
    lines += [f"- {f}" for f in r["findings"]]
    lines += ["", "### Capital.com nach Strategie"] + table(r["capital"]["by_strategy"], "")
    lines += ["", "### Capital.com nach Markt"] + table(r["capital"]["by_market"], "")
    lines += ["", "### Memecoin-Desk nach Signal-Kombination"] + table(r["memecoin"]["by_signals"], " SOL")
    lines += ["", "### Memecoin-Desk nach Ausstiegsgrund"] + table(r["memecoin"]["by_exit"], " SOL")
    lines += ["", "### Schattenbuch (nicht gehandelt, Spread abgezogen)"]
    best = r["shadow"]["best"]
    lines += [f"- {g['epic']} {g['strategy']} {g['resolution']}: {g['n']} Signale, {g['expectancy_r']:+.2f} R, "
              f"Treffer {g['win_rate']:.0%}" for g in best] or ["- keine Gruppe mit genug Signalen"]
    lines += ["", "Vergangene Ergebnisse sind kein Versprechen fuer die Zukunft."]
    return "\n".join(lines)
