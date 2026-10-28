"""Forschungslauf von Hand: Historie laden -> alle Kandidaten kostenrealistisch walk-forward pruefen -> Urteil schreiben.

    python scripts/research_run.py                       # alle Whitelist-Maerkte, 1 Stunde, 365 Tage
    python scripts/research_run.py BTCUSD EURUSD --days 365 --resolution HOUR
    python scripts/research_run.py --offline data/history  # nur vorhandene CSVs auswerten

Der Orchestrator macht das inzwischen automatisch (beim Start ohne gueltiges Urteil und dann woechentlich).
Schreibt data/strategy_verdicts.json (Tor fuer den Desk) und data/research_report.md."""
import argparse
import asyncio
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.firm.research import history, pipeline  # noqa: E402


async def load_online(epics, resolution, days):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from src.firm.broker.client import CapitalComClient
    client = CapitalComClient()
    await client.authenticate()
    try:
        return await pipeline.fetch_history(client, epics, resolution, days, ROOT / "data" / "history")
    finally:
        await client.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("epics", nargs="*")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--resolution", default="HOUR")
    ap.add_argument("--offline", default=None, help="Ordner mit CSVs statt Download")
    a = ap.parse_args()
    epics = a.epics or list(tomllib.loads((ROOT / "config" / "whitelist.toml").read_text()).get("allowed_epics", []))
    if a.offline:
        data = {p.stem.split("_")[0]: history.load_csv(p) for p in Path(a.offline).glob(f"*_{a.resolution}.csv")}
    else:
        data = asyncio.run(load_online(epics, a.resolution, a.days))
    verdicts_path = ROOT / "data" / "strategy_verdicts.json"
    verdicts, lines, trials = pipeline.run_research(data, a.resolution, pipeline.previous_trials(verdicts_path))
    pipeline.write_outputs(verdicts, lines, trials, a.resolution, ROOT / "data")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
