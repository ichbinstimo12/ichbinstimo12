"""Forschungslauf: Historie laden -> alle Kandidaten kostenrealistisch walk-forward pruefen -> Urteil schreiben.

    python scripts/research_run.py                       # alle Whitelist-Maerkte, 1 Stunde, 180 Tage
    python scripts/research_run.py BTCUSD EURUSD --days 365 --resolution HOUR
    python scripts/research_run.py --offline data/history  # nur vorhandene CSVs auswerten

Schreibt data/strategy_verdicts.json (Tor fuer den Desk) und data/research_report.md."""
import argparse
import asyncio
import json
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.firm.research import history, lab  # noqa: E402


async def load_online(epics, resolution, days):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from src.firm.broker.client import CapitalComClient
    client = CapitalComClient()
    await client.authenticate()

    async def page(epic, res, a, b):
        r = await client._request("GET", f"/prices/{epic}", params={"resolution": res, "from": a, "to": b, "max": 1000})
        return r.json().get("prices") or []
    out = {}
    for e in epics:
        print(f"Lade {e} ({days} Tage, {resolution}) ...", flush=True)
        c = await history.afetch_range(page, e, resolution, days)
        history.save_csv(ROOT / "data" / "history" / f"{e}_{resolution}.csv", c)
        out[e] = c
    await client.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("epics", nargs="*")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--resolution", default="HOUR")
    ap.add_argument("--offline", default=None, help="Ordner mit CSVs statt Download")
    a = ap.parse_args()
    epics = a.epics or list(tomllib.loads((ROOT / "config" / "whitelist.toml").read_text()).get("allowed_epics", []))
    if a.offline:
        data = {p.stem.split("_")[0]: history.load_csv(p) for p in Path(a.offline).glob("*.csv")}
    else:
        data = asyncio.run(load_online(epics, a.resolution, a.days))
    verdicts, lines, trials = {}, ["# Forschungsbericht", f"Kerzen: {a.resolution}, Kosten inkl. Spread/Slippage, Walk-Forward (5 Folds)\n"], 0
    for epic, candles in data.items():
        q = history.quality_report(candles, a.resolution)
        lines.append(f"## {epic}  ({q['bars']} Kerzen, {q['span_days']:.0f} Tage, Luecken {q['big_gaps']}, mittl. Spread {q['avg_spread_pct']:.3f}%)")
        reps, trials = lab.research(epic, candles, trial_counter_in=trials)
        best = None
        for r in reps:
            lines.append(f"- **{r['strategy']}**: {r['verdict']}" + (f" | OOS {r['oos']['trades']} Trades, E={r['oos']['expectancy_r']:.2f} R, "
                         f"PF={r['oos']['profit_factor'] or 0:.2f}, DD={r['oos']['max_dd_pct']:.1f}% | " if r.get("oos") else " | ") + "; ".join(r["reasons"]))
            rank = {"PASS": 2, "FORWARD_TEST": 1}.get(r["verdict"], 0)
            if rank and (best is None or rank > best[0] or (rank == best[0] and r["oos"]["expectancy_r"] > best[1]["oos"]["expectancy_r"])):
                best = (rank, r)
        if best:
            r = best[1]
            params = r["folds"][-1].get("params") if r.get("folds") else None
            verdicts[epic] = {"verdict": r["verdict"], "strategy": r["strategy"], "params": params, "reasons": r["reasons"],
                              "oos_expectancy_r": r["oos"]["expectancy_r"], "oos_trades": r["oos"]["trades"]}
        else:
            verdicts[epic] = {"verdict": "FAIL", "reasons": ["keine Strategie bestand die Pruefung"]}
        lines.append("")
    lines.append(f"Gesamtzahl getesteter Varianten (Trial-Counter): {trials}. Die Signifikanz-Huerde steigt damit mit jedem Versuch.")
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "strategy_verdicts.json").write_text(json.dumps({"created": time.time(), "trials": trials, "verdicts": verdicts}, indent=2))
    (ROOT / "data" / "research_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
