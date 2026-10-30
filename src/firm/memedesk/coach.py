"""Journal & Coach: woechentliche Auswertung und begrenzte Selbstverbesserung des Memecoin-Desks.

Regeln (Spezifikation):
  * Der Coach aendert NUR Gewichte und Schwellenwerte der Signale ([weights], [thresholds]) und NUR innerhalb von
    [bounds] in config/memedesk.toml. Risikoregeln (config/memedesk_limits.toml) liest er nicht einmal.
  * Eine Aenderung gibt es nur nach Backtest auf den eigenen Journal-Daten: abgeschlossene Trades mit den beim Einstieg
    gespeicherten Kennzahlen werden mit den neuen Schwellen neu bewertet ("haette der Desk ihn genommen?").
  * Mindestens MIN_TRADES Trades, mindestens die Haelfte bleibt erhalten, Verbesserung >= MIN_IMPROVEMENT
    Prozentpunkte im Schnitt. Sonst: keine Aenderung (und das wird ebenfalls protokolliert).
  * Jede Aenderung wird versioniert (data/memedesk/params_vN.json) und mit Diff als MEME_COACH_CHANGE gemeldet.
"""
from __future__ import annotations

import itertools
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MIN_TRADES = 30
MIN_IMPROVEMENT = 2.0       # Prozentpunkte durchschnittlicher Trade-P&L
GRID_STEPS = 4


def load_closed_trades(journal: Path) -> List[Dict[str, Any]]:
    if not journal.exists():
        return []
    rows = [json.loads(l) for l in journal.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in rows if r.get("type") == "CLOSE" and r.get("metrics") and isinstance(r.get("pnl_pct"), (int, float))]


def would_enter(metrics: Dict[str, Any], signals: Dict[str, bool], th: Dict[str, float]) -> bool:
    """Einstiegsentscheidung mit anderen Schwellen aus den beim Einstieg gespeicherten Kennzahlen."""
    sig = {
        "narrative": float(metrics.get("narrative") or 0) >= th["narrative_min"],
        "volume_holders": float(metrics.get("volume_growth") or 0) >= th["volume_growth_min"]
        and float(metrics.get("buy_ratio") or 0) >= th["buy_ratio_min"]
        and (metrics.get("holders_growth") is None or float(metrics["holders_growth"]) > 0),
        "smart_money": metrics.get("smart_buyers") is not None and int(metrics["smart_buyers"]) >= th["smart_wallets_min"],
        "distribution": bool(signals.get("distribution")),
        "structure": bool(signals.get("structure")),
    }
    chasing = float(metrics.get("m5_change") or 0) > th["chase_m5_pct"]
    no_flow = bool(th.get("require_flow", True)) and not (sig["volume_holders"] or sig["smart_money"])
    return sum(sig.values()) >= int(th["min_signals"]) and not chasing and not no_flow


def backtest(trades: List[Dict[str, Any]], th: Dict[str, float]) -> Tuple[int, Optional[float]]:
    taken = [t["pnl_pct"] for t in trades if would_enter(t["metrics"], t.get("signals") or {}, th)]
    return len(taken), (sum(taken) / len(taken) if taken else None)


def _grid(lo: float, hi: float) -> List[float]:
    return [round(lo + (hi - lo) * i / GRID_STEPS, 4) for i in range(GRID_STEPS + 1)]


def suggest(trades: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    th, bounds = dict(cfg["thresholds"]), cfg["bounds"]
    if len(trades) < MIN_TRADES:
        return {"change": False, "reason": f"zu wenig Daten: {len(trades)} abgeschlossene Trades (mind. {MIN_TRADES})"}
    n0, base = backtest(trades, th)
    tunable = [k for k in ("narrative_min", "volume_growth_min", "buy_ratio_min", "chase_m5_pct") if k in bounds]
    best = (base if base is not None else -1e9, th, n0)
    for combo in itertools.product(*(_grid(*bounds[k]) for k in tunable)):
        cand = dict(th, **dict(zip(tunable, combo)))
        n, avg = backtest(trades, cand)
        if avg is None or n < max(MIN_TRADES // 2, n0 // 2):
            continue
        if avg > best[0] + 1e-9:
            best = (avg, cand, n)
    weights = dict(cfg["weights"])
    for k in weights:   # weight ~ how much better trades were when this signal fired (bounded)
        on = [t["pnl_pct"] for t in trades if (t.get("signals") or {}).get(k)]
        off = [t["pnl_pct"] for t in trades if not (t.get("signals") or {}).get(k)]
        if len(on) >= 5 and len(off) >= 5:
            lo, hi = bounds[k]
            weights[k] = round(min(hi, max(lo, 1 + (sum(on) / len(on) - sum(off) / len(off)) / 50)), 3)
    improved = base is None or best[0] - base >= MIN_IMPROVEMENT
    new_th = best[1] if improved else th
    diff = {k: (cfg["thresholds"][k], new_th[k]) for k in tunable if new_th[k] != cfg["thresholds"][k]}
    diff.update({k: (cfg["weights"][k], v) for k, v in weights.items() if v != cfg["weights"][k]})
    if not diff:
        return {"change": False, "reason": f"keine Verbesserung >= {MIN_IMPROVEMENT} Pp. (Basis {base:.1f} % "
                                           f"ueber {n0} Trades)" if base is not None else "keine Verbesserung"}
    return {"change": True, "thresholds": {k: new_th[k] for k in tunable}, "weights": weights, "diff": diff,
            "backtest": {"trades": len(trades), "baseline_avg_pct": base, "baseline_taken": n0,
                         "new_avg_pct": best[0] if improved else base, "new_taken": best[2] if improved else n0}}


def run_coach(cfg: Dict[str, Any], journal: Path, data_dir: Path, event_store=None, now_fn=time.time) -> Dict[str, Any]:
    trades = load_closed_trades(journal)
    result = suggest(trades, cfg)
    if result["change"]:
        data_dir.mkdir(parents=True, exist_ok=True)
        versions = sorted(data_dir.glob("params_v*.json"))
        version = len(versions) + 1
        doc = {"version": version, "created": now_fn(), "thresholds": result["thresholds"], "weights": result["weights"],
               "diff": result["diff"], "backtest": result["backtest"]}
        (data_dir / f"params_v{version}.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
        (data_dir / "params_current.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
        result["version"] = version
    if event_store is not None:
        event_store.append("MEME_COACH", "MEME_COACH_CHANGE" if result["change"] else "MEME_COACH_REVIEW",
                           {k: v for k, v in result.items() if k != "weights"} | {"trades": len(trades)})
    return result
