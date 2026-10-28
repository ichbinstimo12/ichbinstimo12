"""Tor zwischen Forschung und Handel: Nur Strategien mit Urteil handeln, FAIL/unbekannt = kein Handel.
research_run.py schreibt data/strategy_verdicts.json. PASS = normales Risiko, FORWARD_TEST = nur Demo, halbes Risiko."""
from __future__ import annotations

import json
import time
from pathlib import Path


class StrategyGate:
    def __init__(self, path="data/strategy_verdicts.json", max_age_days=30):
        self.path, self.max_age = Path(path), max_age_days * 86400

    def _load(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def decision(self, epic: str, is_demo: bool):
        """-> (erlaubt, strategie, params, risk_factor, grund)"""
        d = self._load()
        v = d.get("verdicts", {}).get(epic)
        if not v:
            return False, None, None, 0.0, f"kein Forschungsurteil fuer {epic} (erst scripts/research_run.py ausfuehren)"
        if time.time() - d.get("created", 0) > self.max_age:
            return False, None, None, 0.0, "Forschungsurteil aelter als 30 Tage (neu pruefen)"
        verdict = v["verdict"]
        if verdict == "PASS":
            return True, v["strategy"], v.get("params"), 1.0, "PASS (out-of-sample bestanden)"
        if verdict == "FORWARD_TEST" and is_demo:
            return True, v["strategy"], v.get("params"), 0.5, "FORWARD_TEST (nur Demo, halbes Risiko)"
        return False, None, None, 0.0, f"Urteil {verdict}: " + "; ".join(v.get("reasons", [])[:2])
