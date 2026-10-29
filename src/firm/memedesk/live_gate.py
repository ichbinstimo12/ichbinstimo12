"""Freischalt-Pruefung fuer LIVE (Spezifikation): >= min_paper_days PAPER, >= min_paper_trades Trades, Ergebnis positiv.
Auch wenn alles erfuellt ist, bleibt LIVE in diesem Stand gesperrt: der LIVE-Executor (Jupiter-Swap signieren mit
verschluesseltem Schluessel, App-Bestaetigung je Kauf) ist noch nicht gebaut - siehe docs/memedesk.md."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Tuple


def paper_record(journal: Path) -> Dict[str, Any]:
    rows = [json.loads(l) for l in journal.read_text(encoding="utf-8").splitlines() if l.strip()] if journal.exists() else []
    closes = [r for r in rows if r.get("type") == "CLOSE" and r.get("mode", "PAPER") == "PAPER"]
    opens = [r for r in rows if r.get("type") == "OPEN" and r.get("mode", "PAPER") == "PAPER"]
    first = min((r["ts"] for r in opens), default=None)
    pnl = sum(float(r.get("pnl_sol") or 0) for r in closes)
    return {"trades": len(closes), "first_trade_ts": first, "pnl_sol": round(pnl, 6),
            "days": round((time.time() - first) / 86400, 1) if first else 0.0}


def check(journal: Path, limits: Dict[str, Any], state_path: Path) -> Tuple[bool, str]:
    rec, live = paper_record(journal), limits["live"]
    missing = []
    if rec["days"] < live["min_paper_days"]:
        missing.append(f"{rec['days']} von {live['min_paper_days']} Tagen PAPER")
    if rec["trades"] < live["min_paper_trades"]:
        missing.append(f"{rec['trades']} von {live['min_paper_trades']} Trades")
    if rec["pnl_sol"] <= 0:
        missing.append(f"PAPER-Ergebnis {rec['pnl_sol']:+.4f} SOL (muss positiv sein)")
    if missing:
        return False, "; ".join(missing)
    return True, ("PAPER-Kriterien erfuellt. Der LIVE-Executor ist in diesem Stand noch nicht gebaut - "
                  "melde dich, dann wird er mit verschluesseltem Schluessel und App-Bestaetigung ergaenzt.")
