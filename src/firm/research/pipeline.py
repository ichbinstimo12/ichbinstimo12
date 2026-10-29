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


LANE_RISK = {"PASS": 1.0, "FORWARD_TEST": 0.5, "EXPLORE": 0.25}


def size_feasibility(report: dict, market: Optional[dict], account: Optional[dict], lane: str) -> Optional[str]:
    """Kann das Konto die Strategie mit der Mindestgroesse des Brokers ueberhaupt innerhalb des Risikobudgets handeln?
    None = ja oder unbekannt; sonst Begruendung. Rechnung: Mindestgroesse x typischer Stop (Median OOS) x Wechselkurs."""
    if not market or not account:
        return None
    extra = report.get("extra") or {}
    stop_pct = extra.get("median_stop_pct")
    price, min_size, fx = market.get("price"), market.get("min_size"), market.get("fx")
    balance, risk_pct = account.get("balance"), account.get("risk_pct")
    if not all(isinstance(x, (int, float)) and x > 0 for x in (stop_pct or 0, price or 0, min_size or 0, fx or 0,
                                                               balance or 0, risk_pct or 0)):
        return None
    risk_at_min = float(min_size) * float(stop_pct) * float(price) * float(fx)
    budget = float(balance) * float(risk_pct) * LANE_RISK.get(lane, 1.0)
    if risk_at_min > budget:
        return (f"Mindestgroesse {min_size:g} riskiert bei typischem Stop ({stop_pct:.2%}) {risk_at_min:.2f}, "
                f"Budget nur {budget:.2f} - mit diesem Konto nicht handelbar")
    return None


def _table_row(epic: str, resolution: str, r: dict) -> str:
    m, m2, x = r.get("oos") or {}, r.get("oos_cost2") or {}, r.get("extra") or {}
    f = lambda v, fmt: (fmt % v) if isinstance(v, (int, float)) else "—"
    wr = m.get("win_rate")
    return (f"| {r['strategy']} | {epic} | {resolution} | {f(x.get('sharpe_annual'), '%.2f')} | "
            f"{f(m.get('max_dd_pct'), '%.1f%%')} | {m.get('trades', 0)} | {f(wr * 100 if wr is not None else None, '%.0f%%')} | "
            f"{f(m.get('profit_factor'), '%.2f')} | {f(m.get('expectancy_r'), '%.2f')} | {f(m2.get('expectancy_r'), '%.2f')} | "
            f"{r['verdict']} |")


def load_session(epic: str, path: str | Path = "config/sessions.toml") -> Dict[str, Any]:
    """Daytrading-Handelsfenster je Markt (UTC)."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    out = {"entry_start_h": 7.0, "entry_end_h": 19.0, "flat_h": 20.0, "session_tz": ""}
    out.update(data.get("default") or {})
    out.update((data.get("epic") or {}).get(epic) or {})
    return {"intraday": True, **{k: (str(v) if k == "session_tz" else float(v)) for k, v in out.items()}}


def _final_params(name: str, report: dict, candles: list, base: Optional[dict] = None) -> Optional[dict]:
    """Parameter fuer den Live-Desk (inkl. Sitzungsfenster). Lernende Modelle werden auf der GESAMTEN Historie neu
    trainiert und mit ihren Gewichten exportiert, damit Desk und Labor dieselbe Entscheidung treffen."""
    params = report["folds"][-1].get("params") if report.get("folds") else None
    merged = {**(base or {}), **(params or {})}
    strat = lab.REGISTRY[name](**merged)
    if hasattr(strat, "export"):
        ind = lab.Ind(lab.normalize(candles))
        strat.prepare(ind, ind.n)
        if not strat.weights:
            return None
        return {**(base or {}), **strat.export()}
    return merged


def run_research(data: Dict[str, list], resolution: str, trials_in: int = 0, log=print,
                 markets: Optional[Dict[str, dict]] = None,
                 account: Optional[dict] = None) -> Tuple[Dict[str, Any], List[str], int]:
    """Ein Zeitrahmen (Rueckwaertskompatibel). Siehe run_research_multi."""
    return run_research_multi({resolution: data}, trials_in, log, markets, account)


def run_research_multi(data_by_res: Dict[str, Dict[str, list]], trials_in: int = 0, log=print,
                       markets: Optional[Dict[str, dict]] = None,
                       account: Optional[dict] = None) -> Tuple[Dict[str, Any], List[str], int]:
    """CPU-Teil (synchron). `data_by_res`: Zeitrahmen -> Markt -> Kerzen. Unter einer Stunde wird Daytrading geprueft
    (Kandidaten mit Handelsfenster, Glattstellung vor Sitzungsende), sonst Swing. Je Markt gewinnt der beste bestandene
    Kandidat ueber ALLE Zeitrahmen; jeder Zeitrahmen erhoeht den Trial-Counter (ehrliche Huerde).
    `markets` (epic -> price, min_size, fx) und `account` (balance, risk_pct) aktivieren die Mindestgroessen-Pruefung.
    Rueckgabe: (verdicts, berichtszeilen, trial_counter)."""
    verdicts: Dict[str, Any] = {}
    resolutions = list(data_by_res)
    lines = ["# Forschungsbericht",
             f"Zeitrahmen: {', '.join(resolutions)} (unter 1 Stunde = Daytrading, glattgestellt vor Sitzungsende). "
             f"Kosten inkl. echtem Spread, Slippage und Uebernacht-Finanzierung (ANNAHME, config/costs.toml), "
             f"Walk-Forward (5 Folds). Trial-Counter vor diesem Lauf: {trials_in}",
             "Widerlegungsversuche je Kandidat (automatisch): doppelte Kosten, Nachbar-Parameter, Bootstrap-Intervall, "
             "Signifikanz gegen die Zahl der Versuche, Drawdown gegen Kontolimit, Regime-Aufteilung.\n"]
    table = ["| Strategy | Markt | Timeframe | Sharpe (ann.) | Max DD | Trades | Trefferquote | PF | E (R) | E 2x Kosten | Verdict |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    trials = trials_in
    epics = list(dict.fromkeys(e for d in data_by_res.values() for e in d))
    for epic in epics:
        all_reps: List[dict] = []
        candles_by_res: Dict[str, list] = {}
        costs = load_costs(epic)
        for resolution in resolutions:
            candles = lab.normalize((data_by_res[resolution] or {}).get(epic) or [])
            if not candles:
                lines.append(f"## {epic} {resolution}\n- keine Kursdaten\n")
                continue
            candles_by_res[resolution] = candles
            intraday = lab.is_intraday_resolution(resolution)
            base = load_session(epic) if intraday else None
            q = history.quality_report(candles, resolution)
            lines.append(f"## {epic} {resolution} ({'Daytrading' if intraday else 'Swing'}; {q['bars']} Kerzen, "
                         f"{q['span_days']:.0f} Tage, Luecken {q['big_gaps']}, mittl. Spread {q['avg_spread_pct'] or 0:.3f}%"
                         + (f", Fenster {base['entry_start_h']:g}-{base['entry_end_h']:g}, flat {base['flat_h']:g} "
                            f"({base.get('session_tz') or 'UTC'})"
                            if base else f", Finanzierung {costs['overnight_pct_day'] * 100:.3f}%/Tag") + ")")
            log(f"Pruefe {epic} {resolution} ({q['bars']} Kerzen) ...")
            reps, trials = lab.research(epic, candles, names=lab.candidates_for(resolution), trial_counter_in=trials,
                                        base=base, slip_atr=costs["slip_atr"],
                                        overnight_pct_day=costs["overnight_pct_day"],
                                        bar_seconds=lab.RES_SECONDS.get(resolution, 3600))
            for r in reps:
                r["resolution"], r["base"] = resolution, base
                if r.get("oos"):
                    table.append(_table_row(epic, resolution, r))
                    reg = (r.get("extra") or {}).get("regimes") or {}
                    if reg:
                        lines.append(f"  - {r['strategy']} Regime: " + ", ".join(
                            f"{k} n={v['n']} E={v['expectancy_r']:.2f}R" if v.get("expectancy_r") is not None else f"{k} n=0"
                            for k, v in reg.items()))
                oos = r.get("oos")
                lines.append(f"- **{r['strategy']}**: {r['verdict']}"
                             + (f" | OOS {oos['trades']} Trades, E={oos['expectancy_r']:.2f} R, PF={oos['profit_factor'] or 0:.2f}, "
                                f"DD={oos['max_dd_pct']:.1f}%, 2x Kosten E={r['oos_cost2']['expectancy_r']:.2f} R | " if oos else " | ")
                             + "; ".join(r["reasons"]))
            all_reps += reps
        best, explore = _pick(all_reps)
        mk = (markets or {}).get(epic)
        entry: Dict[str, Any]
        if best:
            params = _final_params(best["strategy"], best, candles_by_res[best["resolution"]], best["base"])
            entry = {"verdict": best["verdict"], "strategy": best["strategy"], "params": params,
                     "resolution": best["resolution"], "reasons": best["reasons"],
                     "oos_expectancy_r": best["oos"]["expectancy_r"], "oos_trades": best["oos"]["trades"],
                     "backtest_max_dd_r": best["oos"].get("max_dd_r"),
                     "median_stop_pct": (best.get("extra") or {}).get("median_stop_pct")}
            why_size = size_feasibility(best, mk, account, best["verdict"])
            if why_size:
                entry.update(size_feasible=False, size_reason=why_size)
                lines.append(f"- {epic}: NICHT HANDELBAR: {why_size}")
        else:
            entry = {"verdict": "FAIL", "reasons": ["keine Strategie bestand die Pruefung"]}
        if explore and (not best or best["verdict"] != "PASS"):
            params = _final_params(explore["strategy"], explore, candles_by_res[explore["resolution"]], explore["base"])
            why_size = size_feasibility(explore, mk, account, "EXPLORE")
            if why_size:
                lines.append(f"- {epic}: EXPLORE nicht handelbar: {why_size}")
            elif params is not None:
                entry["explore"] = {"strategy": explore["strategy"], "params": params,
                                    "resolution": explore["resolution"],
                                    "oos_expectancy_r": explore["oos"]["expectancy_r"],
                                    "oos_trades": explore["oos"]["trades"]}
                lines.append(f"- {epic}: EXPLORE (nur Demo, 1/4 Risiko): {explore['strategy']} {explore['resolution']} "
                             f"(OOS E={explore['oos']['expectancy_r']:.2f} R, {explore['oos']['trades']} Trades, nicht signifikant)")
        verdicts[epic] = entry
        lines.append("")
    lines += ["", "## Verdict-Tabelle (alle Zahlen Out-of-Sample, berechnet)", *table, "",
              f"Gesamtzahl getesteter Varianten (Trial-Counter, fortgeschrieben): {trials}."]
    return verdicts, lines, trials


def write_outputs(verdicts: Dict[str, Any], lines: List[str], trials: int, resolution,
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
            txt += (f" ({v['strategy']} {v.get('resolution') or ''}, OOS E={v.get('oos_expectancy_r', 0):.2f} R, "
                    f"n={v.get('oos_trades')})")
        if v.get("explore"):
            txt += f"; EXPLORE {v['explore']['strategy']}"
        out[epic] = txt
    return out
