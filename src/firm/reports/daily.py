"""Taeglicher Bericht aus echten Daten (Event-Log). Ersetzt den alten Platzhalter mit erfundenen Token-Kosten.

Inhalt: Konto (Equity Start/Ende/Drawdown aus Broker-Snapshots), Fills, geschlossene Trades mit P&L und R,
Ablehnungen nach Grund, Forschungsstand, Agenten-Kennzahlen, Alarme. Fehlende Werte erscheinen als "—".
Kein Gewinnversprechen, keine Prognose.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.firm.agents.metrics import agent_metrics
from src.firm.bus.event_store import EventStore

logger = logging.getLogger("DailyReporter")


def _p(e) -> Dict[str, Any]:
    try:
        return json.loads(e.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}


def _fmt(v, nd=2) -> str:
    return f"{v:,.{nd}f}" if isinstance(v, (int, float)) else "—"


class DailyReporter:
    def __init__(self, event_store: EventStore, out_dir: str | Path = "reports"):
        self.event_store = event_store
        self.out_dir = Path(out_dir)

    def generate_report(self, day: Optional[str] = None) -> Dict[str, Any]:
        """Report for one UTC day (default: yesterday)."""
        day = day or (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        start = f"{day}T00:00:00"
        end = (datetime.fromisoformat(start) + timedelta(days=1)).isoformat()
        events = [e for e in self.event_store.get_events(after_ts=start) if e["ts"] < end]
        by = lambda t: [e for e in events if e["type"] == t]

        eq = [_p(e).get("equity") for e in by("ACCOUNT_SNAPSHOT") if isinstance(_p(e).get("equity"), (int, float))]
        dd = 0.0
        peak = eq[0] if eq else None
        for v in eq:
            peak = max(peak, v)
            dd = max(dd, (peak - v) / peak if peak else 0.0)
        results = [_p(e) for e in by("TRADE_RESULT")]
        rs = [r["r"] for r in results if isinstance(r.get("r"), (int, float))]
        rejections = Counter(str(_p(e).get("reason") or "?")[:80] for e in by("TRADE_REJECTED"))
        metrics = {a: m for a, m in agent_metrics(self.event_store, days=1.0).items()}
        report = {
            "date": day,
            "equity_start": eq[0] if eq else None,
            "equity_end": eq[-1] if eq else None,
            "equity_change": (eq[-1] - eq[0]) if len(eq) > 1 else None,
            "max_intraday_drawdown_pct": dd * 100 if eq else None,
            "fills": len(by("ORDER_FILLED")),
            "closed_trades": len(results),
            "realized_pnl": sum(r["pnl"] for r in results if isinstance(r.get("pnl"), (int, float))) if results else None,
            "expectancy_r": sum(rs) / len(rs) if rs else None,
            "rejections": dict(rejections.most_common(8)),
            "halts": [_p(e).get("reason") for e in by("TRADING_HALTED")],
            "strategy_stops": [f"{_p(e).get('epic')}:{_p(e).get('strategy')}" for e in by("STRATEGY_STOPPED")],
            "research": [e["type"] for e in events if e["type"].startswith("RESEARCH_")],
            "agents": {a: {k: m.get(k) for k in ("tasks", "valid_rate", "invalid", "actions", "tokens")}
                       for a, m in metrics.items()},
            "note": "Kennzahlen eines Tages sagen nichts ueber einen Edge aus (zu wenige Trades).",
        }
        return report

    def to_markdown(self, r: Dict[str, Any]) -> str:
        lines = [f"# Tagesbericht {r['date']} (UTC)", "",
                 f"- Equity: {_fmt(r['equity_start'])} -> {_fmt(r['equity_end'])} "
                 f"(Veraenderung {_fmt(r['equity_change'])}, max. Intraday-Drawdown {_fmt(r['max_intraday_drawdown_pct'])} %)",
                 f"- Fills: {r['fills']}, geschlossene Trades: {r['closed_trades']}, realisiert: {_fmt(r['realized_pnl'])}, "
                 f"Erwartungswert: {_fmt(r['expectancy_r'])} R",
                 f"- Halts: {', '.join(map(str, r['halts'])) or 'keine'}; gestoppte Strategien: "
                 f"{', '.join(r['strategy_stops']) or 'keine'}",
                 f"- Forschung: {', '.join(r['research']) or 'kein Lauf'}", "", "## Ablehnungen"]
        lines += [f"- {k}: {v}" for k, v in r["rejections"].items()] or ["- keine"]
        lines += ["", "## Agenten (heute, gemessen)", "| Agent | Aufgaben | gueltig | verworfen | Aktionen | Tokens |",
                  "|---|---|---|---|---|---|"]
        for a, m in sorted(r["agents"].items()):
            lines.append(f"| {a} | {m['tasks']} | {_fmt(m['valid_rate'])} | {m['invalid']} | {m['actions']} | {m['tokens']} |")
        lines += ["", r["note"]]
        return "\n".join(lines)

    def save_to_disk(self, report: Dict[str, Any]) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"daily_{report['date']}.md"
        path.write_text(self.to_markdown(report), encoding="utf-8")
        (self.out_dir / f"daily_{report['date']}.json").write_text(json.dumps(report, indent=2, default=str))
        logger.info(f"Daily report saved to {path}")
        return path
