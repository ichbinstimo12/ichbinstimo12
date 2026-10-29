"""PAPER-Testlauf des Memecoin-Desks gegen die echten Datenquellen (DexScreener, RugCheck, Jupiter, GeckoTerminal).

Laeuft isoliert (eigener Ordner, eigene Datenbank, eigenes Spielgeld), sendet keine Transaktionen und schreibt am Ende
ein lesbares Protokoll aller Agenten-Entscheidungen.

    python scripts/memedesk_paper_run.py --minutes 10 --out reports/memedesk_paper_run
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.firm.envload import use_os_trust_store  # noqa: E402

use_os_trust_store()

from src.firm.bus.event_store import EventStore  # noqa: E402
from src.firm.memedesk.desk import MemeDesk  # noqa: E402
from src.firm.memedesk.logic import load_config, load_limits  # noqa: E402
from src.firm.memedesk.sources import MemeSources  # noqa: E402

SKIP_IN_LOG = {"MEME_NARRATIVE", "MEME_WATCHLIST"}


async def run(out: Path, seconds: float) -> None:
    cfg = load_config()
    src = MemeSources(cfg)
    store = EventStore(str(out / "events.db"))
    desk = MemeDesk(src, store, cfg=cfg, limits=load_limits(), data_dir=out / "data", journal_dir=out / "journal")
    end = time.time() + seconds
    try:
        await desk.run(lambda: time.time() < end)
    finally:
        await src.close()
    print("Quellen-Fehler:", src.errors or "keine")


def write_log(out: Path) -> Path:
    db = sqlite3.connect(out / "events.db")
    rows = db.execute("SELECT ts, agent_id, type, payload_json FROM events ORDER BY ts").fetchall()
    lines = [f"# Memecoin-Desk PAPER-Testlauf ({len(rows)} Ereignisse)", "",
             "Zaehler: " + ", ".join(f"{k} {v}" for k, v in collections.Counter(r[2] for r in rows).items()), ""]
    for ts, agent, kind, payload in rows:
        if kind in SKIP_IN_LOG:
            continue
        p = json.loads(payload)
        text = p.get("reason") or "; ".join(p.get("reasons") or []) or p.get("action") or p.get("error") or ""
        if kind == "MEME_SCAN":
            text = f"{p['seen']} gesehen, {p['checked']} geprueft, {p['passed_filter']} im Filter, aussortiert {p['rejected']}"
        sym = p.get("symbol") or ""
        extra = f" · {p['size_sol']} SOL" if p.get("size_sol") else ""
        extra += f" · {p['pnl_pct']:+.1f} %" if isinstance(p.get("pnl_pct"), (int, float)) else ""
        lines.append(f"- `{ts[11:19]}` **{agent.replace('MEME_', '')}** {kind.replace('MEME_', '')} {sym} {text}{extra}")
    state = json.loads((out / "data" / "state.json").read_text()) if (out / "data" / "state.json").exists() else {}
    lines += ["", f"Endstand: Desk-Kapital {state.get('equity_mark', state.get('equity'))} SOL, "
                  f"offene Positionen {list((state.get('positions') or {}).keys())}, "
                  f"abgeschlossene Trades {state.get('closed_trades', 0)}"]
    path = out / "example_log.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--out", default="reports/memedesk_paper_run")
    ap.add_argument("--log-only", action="store_true", help="nur das Protokoll aus einem frueheren Lauf schreiben")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if not a.log_only:
        asyncio.run(run(out, a.minutes * 60))
    print("Protokoll:", write_log(out))


if __name__ == "__main__":
    main()
