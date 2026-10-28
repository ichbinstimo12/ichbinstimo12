"""Forschungspipeline: Historie laden -> alle Kandidaten walk-forward pruefen -> Urteil schreiben.

Wird vom Skript `scripts/research_run.py` UND automatisch vom Orchestrator benutzt (beim Start, wenn kein
gueltiges Urteil existiert, und danach regelmaessig). Vorher musste der Forschungslauf von Hand gestartet werden;
ohne ihn lag kein Urteil vor und das Strategie-Tor hat jeden Handel blockiert.

Regeln:
  * Der Trial-Counter wird ueber alle Laeufe fortgeschrieben (nie zurueckgesetzt), die Signifikanz-Huerde steigt.
  * PASS / FORWARD_TEST entscheidet allein `lab.research`. Ergaenzend gibt es die Spur EXPLORE: nur Demo, kleinstes
    Risiko, nur fuer Kandidaten mit positivem Out-of-Sample-Erwartungswert nach Kosten und genug Trades. Sie dient
    dazu, Vorwaertsdaten zu sammeln, und ist im Live-Modus per Code gesperrt (siehe StrategyGate).
"""
from __future__ import annotations

import json
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import history, lab

EXPLORE_MIN_TRADES = 30
EXPLORE_MIN_EXPECTANCY_R = 0.0


def load_costs(epic: str, path: str | Path = "config/costs.toml") -> Dict[str, float]:
    """Backtest-Kostenannahmen je Instrument (Uebernacht-Finanzierung, Slippage)."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    out = {"overnight_pct_day": 0.0002, "slip_atr": 0.03}
    out.update(data.get("default") or {})
    out.update((data.get("epic") or {}).get(epic) or {})
    return {k: float(v) for k, v in out.items()}


def previous_trials(verdicts_path: str | Path) -> int:
    try:
        return int(json.loads(Path(verdicts_path).read_text()).get("trials") or 0)
    except (OSError, ValueError, TypeError):
        return 0


async def fetch_history(client, epics: List[str], resolution: str, days: int,
                        cache_dir: str | Path = "data/history", log=print) -> Dict[str, list]:
    """Laedt die Historie seitenweise vom Broker und legt sie als CSV ab."""
    async def page(epic, res, a, b):
        return await client.get_prices_range(epic, res, a, b)

    out: Dict[str, list] = {}
    for e in epics:
        log(f"Lade {e} ({days} Tage, {resolution}) ...")
        candles = await history.afetch_range(page, e, resolution, days)
        if candles:
            history.save_csv(Path(cache_dir) / f"{e}_{resolution}.csv", candles)
        out[e] = candles
    return out


def _pick(reports: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    """Beste Freigabe (PASS > FORWARD_TEST, dann Erwartungswert) und bester EXPLORE-Kandidat."""
    best = None
    for r in reports:
        rank = {"PASS": 2, "FORWARD_TEST": 1}.get(r.get("verdict"), 0)
        if rank and (best is None or rank > best[0]
                     or (rank == best[0] and r["oos"]["expectancy_r"] > best[1]["oos"]["expectancy_r"])):
            best = (rank, r)
    explore = None
    for r in reports:
        m, m2 = r.get("oos") or {}, r.get("oos_cost2") or {}
        if not m:
            continue
        if (m.get("trades", 0) >= EXPLORE_MIN_TRADES and m.get("expectancy_r", 0) > EXPLORE_MIN_EXPECTANCY_R
                and (m2.get("expectancy_r") or 0) > -0.05 and m.get("max_dd_pct", 99) <= 8.0):
            if explore is None or m["expectancy_r"] > explore["oos"]["expectancy_r"]:
                explore = r
    return (best[1] if best else None), explore


def _final_params(name: str, report: dict, candles: list) -> Optional[dict]:
    """Parameter fuer den Live-Desk. Lernende Modelle werden auf der GESAMTEN Historie neu trainiert und mit
    ihren Gewichten exportiert, damit Desk und Labor dieselbe Entscheidung treffen."""
    params = report["folds"][-1].get("params") if report.get("folds") else None
    strat = lab.REGISTRY[name](**(params or {}))
    if hasattr(strat, "export"):
        ind = lab.Ind(lab.normalize(candles))
        strat.prepare(ind, ind.n)
        if not strat.weights:
            return None
        return strat.export()
    return params


def run_research(data: Dict[str, list], resolution: str, trials_in: int = 0,
                 log=print) -> Tuple[Dict[str, Any], List[str], int]:
    """CPU-Teil (synchron). Rueckgabe: (verdicts, berichtszeilen, trial_counter)."""
    verdicts: Dict[str, Any] = {}
    lines = ["# Forschungsbericht",
             f"Kerzen: {resolution}, Kosten inkl. echtem Spread, Slippage und Uebernacht-Finanzierung (ANNAHME, "
             f"config/costs.toml), Walk-Forward (5 Folds). Trial-Counter vor diesem Lauf: {trials_in}\n"]
    trials = trials_in
    bar_s = lab.RES_SECONDS.get(resolution, 3600)
    for epic, candles in data.items():
        candles = lab.normalize(candles)
        if not candles:
            verdicts[epic] = {"verdict": "INSUFFICIENT_DATA", "reasons": ["keine Kursdaten geladen"]}
            lines.append(f"## {epic}\n- keine Kursdaten\n")
            continue
        q = history.quality_report(candles, resolution)
        costs = load_costs(epic)
        lines.append(f"## {epic}  ({q['bars']} Kerzen, {q['span_days']:.0f} Tage, Luecken {q['big_gaps']}, "
                     f"mittl. Spread {q['avg_spread_pct'] or 0:.3f}%, Finanzierung {costs['overnight_pct_day'] * 100:.3f}%/Tag)")
        log(f"Pruefe {epic} ({q['bars']} Kerzen) ...")
        reps, trials = lab.research(epic, candles, trial_counter_in=trials, slip_atr=costs["slip_atr"],
                                    overnight_pct_day=costs["overnight_pct_day"], bar_seconds=bar_s)
        for r in reps:
            oos = r.get("oos")
            lines.append(f"- **{r['strategy']}**: {r['verdict']}"
                         + (f" | OOS {oos['trades']} Trades, E={oos['expectancy_r']:.2f} R, PF={oos['profit_factor'] or 0:.2f}, "
                            f"DD={oos['max_dd_pct']:.1f}%, 2x Kosten E={r['oos_cost2']['expectancy_r']:.2f} R | " if oos else " | ")
                         + "; ".join(r["reasons"]))
        best, explore = _pick(reps)
        entry: Dict[str, Any]
        if best:
            params = _final_params(best["strategy"], best, candles)
            entry = {"verdict": best["verdict"], "strategy": best["strategy"], "params": params,
                     "reasons": best["reasons"], "oos_expectancy_r": best["oos"]["expectancy_r"],
                     "oos_trades": best["oos"]["trades"]}
        else:
            entry = {"verdict": "FAIL", "reasons": ["keine Strategie bestand die Pruefung"]}
        if explore and (not best or best["verdict"] != "PASS"):
            params = _final_params(explore["strategy"], explore, candles)
            if params is not None or explore["strategy"] != "ml_logit":
                entry["explore"] = {"strategy": explore["strategy"], "params": params,
                                    "oos_expectancy_r": explore["oos"]["expectancy_r"],
                                    "oos_trades": explore["oos"]["trades"]}
                lines.append(f"- EXPLORE (nur Demo, 1/4 Risiko): {explore['strategy']} "
                             f"(OOS E={explore['oos']['expectancy_r']:.2f} R, {explore['oos']['trades']} Trades, nicht signifikant)")
        verdicts[epic] = entry
        lines.append("")
    lines.append(f"Gesamtzahl getesteter Varianten (Trial-Counter, fortgeschrieben): {trials}.")
    return verdicts, lines, trials


def write_outputs(verdicts: Dict[str, Any], lines: List[str], trials: int, resolution: str,
                  out_dir: str | Path = "data") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "strategy_verdicts.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"created": time.time(), "resolution": resolution, "trials": trials,
                               "verdicts": verdicts}, indent=2))
    tmp.replace(path)
    (out / "research_report.md").write_text("\n".join(lines), encoding="utf-8")
    return path


def summarize(verdicts: Dict[str, Any]) -> Dict[str, str]:
    """Kurzfassung je Markt fuer Events/Dashboard/Agenten."""
    out = {}
    for epic, v in verdicts.items():
        txt = v.get("verdict", "?")
        if v.get("strategy"):
            txt += f" ({v['strategy']}, OOS E={v.get('oos_expectancy_r', 0):.2f} R, n={v.get('oos_trades')})"
        if v.get("explore"):
            txt += f"; EXPLORE {v['explore']['strategy']}"
        out[epic] = txt
    return out
