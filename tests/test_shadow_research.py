"""Schattenbuch: gute Signal-Ergebnisse loesen Forschung aus - nie direkt Handel."""
import json
import random
from types import SimpleNamespace

from src.firm.bus.event_store import EventStore
from src.firm.research import shadow_stats


def shadow(store, epic, r, strategy="orb", res="MINUTE_5"):
    store.append("SCANNER", "SHADOW_RESULT", {"epic": epic, "strategy": strategy, "resolution": res, "r": r,
                                              "outcome": "target" if r > 0 else "stop"})


def test_summary_groups_and_needs_enough_evidence(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    random.seed(3)
    for _ in range(40):
        shadow(store, "GOLD", random.choice([1.6, 1.6, -1.05]))
    for _ in range(10):
        shadow(store, "OIL", 2.0)                         # stark, aber zu wenige
    for _ in range(40):
        shadow(store, "US500", random.choice([1.0, -1.05]))   # ~0 R nach Kosten
    summ = shadow_stats.summarize(store.get_events(types=["SHADOW_RESULT"]))
    by = {g["epic"]: g for g in summ}
    assert by["GOLD"]["n"] == 40 and by["OIL"]["n"] == 10
    good = [g["epic"] for g in shadow_stats.promising(summ)]
    assert good == ["GOLD"]
    assert shadow_stats.promising(summ, {"GOLD": {"verdict": "PASS"}}) == []


def test_invalid_outcomes_are_ignored():
    evs = [{"payload_json": json.dumps({"epic": "X", "r": 0.0, "outcome": "invalid"})}, {"payload_json": "kaputt"}]
    assert shadow_stats.summarize(evs) == []


def test_orchestrator_requests_research_once_and_adds_market_to_candidates(tmp_path):
    from src.firm import main as firm_main
    store = EventStore(str(tmp_path / "e.db"))
    for i in range(35):
        shadow(store, "GOLD", 1.6 if i % 3 else -1.05)
    orch = firm_main.FirmOrchestrator.__new__(firm_main.FirmOrchestrator)
    orch.event_store, orch.whitelist = store, ["EURUSD"]
    orch.gate = SimpleNamespace(overview=lambda: {"verdicts": {}})
    why = orch.shadow_research_due()
    assert why and "GOLD" in why
    assert orch.scanner_candidates()[0] == "GOLD"
    store.append("RESEARCH_LAB", "RESEARCH_STARTED", {"epics": ["EURUSD", "GOLD"]})
    assert orch.shadow_research_due() is None       # schon untersucht -> kein Dauer-Neustart
