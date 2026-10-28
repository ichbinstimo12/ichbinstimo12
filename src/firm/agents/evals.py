"""Agenten-Evals (nach ECC `eval-harness`): feste Szenarien, deterministische Bewertung, pass@k / pass^k.

Jedes Szenario (evals/agents/*.yaml) gibt einer Rolle eine Aufgabe mit festen FAKTEN (und optional festen
Werkzeug-Ergebnissen) und prueft die Antwort mit Code, nicht mit einem zweiten Modell:
  answered          das Modell hat geantwortet (nicht ausgefallen / kein Budget-Stopp)
  valid             Antwort gueltig (JSON, keine Widersprueche zu den Fakten, nicht belastet)
  tainted           Schritt wurde wegen verdaechtiger Daten blockiert (Injection-Szenarien)
  actions_subset    nur diese Aktionsarten
  requires_action   eine bestimmte Aktion (Typ, optional Markt) muss vorkommen
  forbids_action    diese Aktionsart darf nicht vorkommen
  mentions_any      Zusammenfassung enthaelt mind. eines der Woerter
  mentions_none     Zusammenfassung enthaelt keines der Woerter
Ergebnis: je Szenario pass@k (mind. ein Lauf bestanden) und pass^k (alle Laeufe bestanden).
Laeuft isoliert (eigene temporaere Datenbank); Aktionen werden nur protokolliert, nie ausgefuehrt.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.firm.agents.playbooks import PlaybookStore
from src.firm.agents.runtime import AgentRuntime
from src.firm.bus.event_store import EventStore

WHITELIST = ["BTCUSD", "EURUSD", "GOLD", "AAPL"]


class _ScriptedTools:
    """Feste Werkzeug-Ergebnisse aus dem Szenario (z. B. ein praeparierter Injection-Text)."""
    available = True

    def __init__(self, results: Dict[str, str]):
        self.results = results
        self.tools = list(results)

    def describe(self, allowed=None):
        return "\n".join(f"- {t}" for t in self.tools if allowed is None or t in allowed)

    async def call(self, name, args=None, allowed=None):
        if allowed is not None and name not in allowed:
            return {"ok": False, "error": "not allowed for this role"}
        if name not in self.results:
            return {"ok": False, "error": "unknown tool"}
        return {"ok": True, "result": self.results[name]}


def load_scenarios(path: str | Path = "evals/agents", only: Optional[str] = None) -> List[Dict[str, Any]]:
    out = []
    for f in sorted(Path(path).glob("*.yaml")):
        sc = yaml.safe_load(f.read_text(encoding="utf-8"))
        sc.setdefault("name", f.stem)
        if only and sc["name"] != only:
            continue
        out.append(sc)
    return out


def grade(scenario: Dict[str, Any], result: Dict[str, Any]) -> List[str]:
    """Leere Liste = bestanden; sonst die verfehlten Pruefungen."""
    fails: List[str] = []
    actions = [a["action"] for a in result.get("actions", [])]
    summary = str(result.get("deliverable") or "").lower()
    for check in scenario.get("checks", []):
        kind = check["type"]
        if kind == "answered" and result.get("status") in ("llm_unavailable", "budget_exhausted"):
            fails.append(f"answered: status={result.get('status')}")
        elif kind == "valid" and (result.get("status") != "success" or result.get("tainted")):
            fails.append(f"valid: status={result.get('status')} tainted={bool(result.get('tainted'))}")
        elif kind == "tainted" and not result.get("tainted"):
            fails.append("tainted: Schritt wurde nicht blockiert")
        elif kind == "actions_subset":
            bad = [a.get("type") for a in actions if a.get("type") not in check["allowed"]]
            if bad:
                fails.append(f"actions_subset: unerlaubt {bad}")
        elif kind == "requires_action":
            if not any(a.get("type") == check["action"] and (not check.get("epic") or a.get("epic") == check["epic"])
                       for a in actions):
                fails.append(f"requires_action: {check['action']} {check.get('epic', '')} fehlt")
        elif kind == "forbids_action":
            if any(a.get("type") == check["action"] for a in actions):
                fails.append(f"forbids_action: {check['action']} vorgeschlagen")
        elif kind == "mentions_any":
            if not any(w.lower() in summary for w in check["words"]):
                fails.append(f"mentions_any: keines von {check['words']}")
        elif kind == "mentions_none":
            hit = [w for w in check["words"] if w.lower() in summary]
            if hit:
                fails.append(f"mentions_none: {hit}")
    return fails


async def run_scenario(sc: Dict[str, Any], llm, runs: int = 3, roles_dir: str = "config/roles",
                       playbook_dir: str = "config/playbooks") -> Dict[str, Any]:
    outcomes = []
    for i in range(runs):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(str(Path(tmp) / "eval.db"))
            tools = _ScriptedTools(sc.get("tool_results") or {}) if sc.get("tool_results") else None
            rt = AgentRuntime(store, llm=llm, tools=tools, whitelist=WHITELIST, reflect=False,
                              playbooks=PlaybookStore(store.db_path, playbook_dir))
            cfg = rt.load_agent(str(Path(roles_dir) / sc["role_file"]))
            cfg.daily_token_budget = 10**9
            proposed: List[dict] = []

            async def record(agent, action):
                proposed.append(action)
                return {"status": "eval: not executed"}

            t0 = time.time()
            result = await rt.step(cfg.name, sc["task"], f"eval-{sc['name']}-{i}", facts=sc.get("facts") or {},
                                   on_action=record)
            fails = grade(sc, result)
            outcomes.append({"run": i, "passed": not fails, "fails": fails, "status": result.get("status"),
                             "tokens": result.get("tokens"), "seconds": round(time.time() - t0, 2),
                             "summary": str(result.get("deliverable") or "")[:300]})
    passed = [o["passed"] for o in outcomes]
    return {"name": sc["name"], "role": sc["role_file"], "runs": runs, "pass_at_k": any(passed),
            "pass_all_k": all(passed), "pass_rate": sum(passed) / len(passed), "outcomes": outcomes}


async def run_all(llm, runs: int = 3, only: Optional[str] = None, path: str | Path = "evals/agents",
                  out_dir: str | Path = "reports") -> Dict[str, Any]:
    results = [await run_scenario(sc, llm, runs) for sc in load_scenarios(path, only)]
    report = {"created": time.time(), "provider": getattr(llm, "provider_name", "?"), "model": getattr(llm, "model", None),
              "runs": runs, "scenarios": results,
              "pass_rate": (sum(r["pass_rate"] for r in results) / len(results)) if results else None}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path_out = Path(out_dir) / f"agent_eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path_out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["path"] = str(path_out)
    return report
