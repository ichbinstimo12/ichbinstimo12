"""Tor zwischen Forschung und Handel: Nur Strategien mit Urteil handeln, FAIL/unbekannt = kein Handel.

Die Forschungspipeline (automatisch oder scripts/research_run.py) schreibt data/strategy_verdicts.json.
  PASS          -> normales Risiko (Demo und Live)
  FORWARD_TEST  -> nur Demo, halbes Risiko
  EXPLORE       -> nur Demo UND nur wenn FIRM_DEMO_EXPLORE nicht abgeschaltet ist, Viertel-Risiko.
                   Positiver, aber nicht signifikanter Out-of-Sample-Erwartungswert. Zweck: Vorwaertsdaten sammeln.
In einem LIVE*-Modus ist alles ausser PASS gesperrt, unabhaengig davon, was der Broker-Client meldet.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

EXPLORE_RISK = 0.25
INTRADAY = ("MINUTE", "MINUTE_5", "MINUTE_15", "MINUTE_30")


def trading_style() -> str:
    """daytrade (Standard): nur Intraday-Zeitrahmen, glattgestellt vor Sitzungsende. swing: auch HOUR/HOUR_4."""
    return "swing" if os.getenv("FIRM_STYLE", "daytrade").strip().lower() == "swing" else "daytrade"


def _explore_enabled() -> bool:
    return os.getenv("FIRM_DEMO_EXPLORE", "1").strip().lower() not in ("0", "false", "no", "off")


class StrategyGate:
    def __init__(self, path="data/strategy_verdicts.json", max_age_days=30):
        self.path, self.max_age = Path(path), max_age_days * 86400

    def _load(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def created(self) -> float:
        return float(self._load().get("created") or 0.0)

    def is_stale(self, max_age_seconds: float | None = None) -> bool:
        created = self.created()
        return not created or time.time() - created > (max_age_seconds or self.max_age)

    def overview(self) -> dict:
        d = self._load()
        return {"created": d.get("created"), "trials": d.get("trials"), "resolution": d.get("resolution"),
                "verdicts": {e: {k: v.get(k) for k in ("verdict", "strategy", "resolution", "oos_expectancy_r",
                                                        "oos_trades", "size_feasible", "size_reason")}
                             | ({"explore": v["explore"]["strategy"]} if v.get("explore") else {})
                             for e, v in (d.get("verdicts") or {}).items()}}

    def decision(self, epic: str, is_demo: bool, mode: str = "PAPER", resolution: str | None = None):
        """-> (erlaubt, strategie, params, risk_factor, grund)"""
        d = self.decide(epic, is_demo, mode, resolution)
        return d["ok"], d["strategy"], d["params"], d["risk_factor"], d["reason"]

    def decide(self, epic: str, is_demo: bool, mode: str = "PAPER", resolution: str | None = None) -> dict:
        """Wie decision(), plus Zeitrahmen der freigegebenen Strategie (`resolution`) und Spur (`lane`)."""
        def no(reason):
            return {"ok": False, "strategy": None, "params": None, "risk_factor": 0.0, "reason": reason,
                    "resolution": None, "lane": None}

        def yes(strategy, params, factor, reason, lane, res):
            if trading_style() == "daytrade" and (res or resolution or "") not in INTRADAY:
                return no(f"Urteil ist Swing ({res or resolution}); Daytrading-Modus (FIRM_STYLE=daytrade) - "
                          "neue Forschung auf Intraday-Kerzen laeuft")
            return {"ok": True, "strategy": strategy, "params": params, "risk_factor": factor, "reason": reason,
                    "resolution": res or default_res, "lane": lane}

        live = str(mode).upper().startswith("LIVE")
        demo_only_ok = is_demo and not live
        d = self._load()
        v = (d.get("verdicts") or {}).get(epic)
        if not v:
            return no(f"kein Forschungsurteil fuer {epic} (Forschungslauf ausstehend)")
        if time.time() - float(d.get("created") or 0) > self.max_age:
            return no("Forschungsurteil aelter als 30 Tage (neu pruefen)")
        file_res = d.get("resolution") if isinstance(d.get("resolution"), str) else None
        default_res = file_res or resolution
        if resolution and file_res and file_res != resolution and not v.get("resolution"):
            return no(f"Urteil gilt fuer {file_res}-Kerzen, Desk laeuft auf {resolution} (FIRM_TRADE_RESOLUTION pruefen)")
        verdict = v.get("verdict")
        if v.get("size_feasible") is False and not v.get("explore"):
            return no(v.get("size_reason") or "mit diesem Konto nicht handelbar (Mindestgroesse)")
        if v.get("size_feasible") is False:
            verdict = "SIZE_BLOCKED"
        if verdict == "PASS":
            return yes(v["strategy"], v.get("params"), 1.0, "PASS (out-of-sample bestanden)", "PASS", v.get("resolution"))
        if verdict == "FORWARD_TEST" and demo_only_ok:
            return yes(v["strategy"], v.get("params"), 0.5, "FORWARD_TEST (nur Demo, halbes Risiko)", "FORWARD_TEST",
                       v.get("resolution"))
        ex = v.get("explore")
        if ex and demo_only_ok and _explore_enabled():
            return yes(ex["strategy"], ex.get("params"), EXPLORE_RISK,
                       f"EXPLORE (nur Demo, 1/4 Risiko, OOS E={ex.get('oos_expectancy_r', 0):.2f} R nicht signifikant)",
                       "EXPLORE", ex.get("resolution"))
        return no(f"Urteil {verdict}: " + "; ".join(v.get("reasons", [])[:2]))
