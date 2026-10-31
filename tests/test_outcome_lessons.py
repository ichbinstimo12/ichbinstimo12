"""Lernen aus echten Ergebnissen: Messwerte werden zu belegten Lektionen der passenden Agenten, bleiben bestaetigt,
solange die Belege gelten, und werden ersetzt (nicht verdoppelt), wenn sich die Zahlen aendern."""
from src.firm.agents.memory import LessonStore
from src.firm.agents.outcome_lessons import refresh
from src.firm.bus.event_store import EventStore


def setup(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    return store, LessonStore(store.db_path)


def trades(store, epic, strat, rs):
    for r in rs:
        store.append("TRADING_DESK", "TRADE_RESULT", {"epic": epic, "strategy": strat, "r": r, "pnl": r * 2})


def lessons_of(mem, agent):
    return [l["lesson"] for l in mem.recent(agent, limit=50)]


def test_live_results_become_evidence_backed_lessons_for_the_right_agents(tmp_path):
    store, mem = setup(tmp_path)
    trades(store, "GOLD", "orb", [-1, -1, -1, 1.5, -1, -1])
    trades(store, "EURUSD", "smc_tjr", [2, -1, 2, 2, -1])
    refresh(store, mem)
    gold = [l for l in lessons_of(mem, "ExecutionTrader") if "GOLD" in l]
    assert gold and "6 live trades" in gold[0] and "-0.58 R" in gold[0]
    assert any("EURUSD" in l for l in lessons_of(mem, "SMCTrader"))           # SMC-Ergebnisse an die SMC-Rollen
    assert not any("GOLD" in l for l in lessons_of(mem, "SMCTrader"))
    row = mem.recent("ExecutionTrader", limit=50)[0]
    assert row["source"] == "outcome" and row["evidence_task_id"]             # Beleg = Ereignis-ID


def test_same_numbers_confirm_changed_numbers_replace(tmp_path):
    store, mem = setup(tmp_path)
    trades(store, "GOLD", "orb", [-1] * 5)
    refresh(store, mem)
    refresh(store, mem)
    assert len([l for l in lessons_of(mem, "RiskOfficer") if "GOLD" in l]) == 1
    trades(store, "GOLD", "orb", [3, 3])
    refresh(store, mem)
    gold = [l for l in lessons_of(mem, "RiskOfficer") if "GOLD" in l]
    assert len(gold) == 1 and "7 live trades" in gold[0]


def test_too_few_trades_teach_nothing_and_reflections_are_kept(tmp_path):
    store, mem = setup(tmp_path)
    mem.add("RiskOfficer", "GOLD spreads widen above 0.2 ATR around 14:30 UTC data releases", evidence_task_id="t1")
    trades(store, "OIL", "orb", [1, 1])
    refresh(store, mem)
    assert lessons_of(mem, "RiskOfficer") == ["GOLD spreads widen above 0.2 ATR around 14:30 UTC data releases"]


def test_blocked_signals_slippage_and_shadow_book_are_learned(tmp_path):
    store, mem = setup(tmp_path)
    for _ in range(6):
        store.append("TRADING_DESK", "SIGNAL_BLOCKED", {"epic": "US500", "stage": "entry-quality",
                                                        "reason": "Spread 0.31 R > 0.20 R - Kosten fressen den Vorteil"})
        store.append("TRADING_DESK", "FILL_QUALITY", {"epic": "US500", "slippage_r": 0.12})
    for i in range(30):
        store.append("SCANNER", "SHADOW_RESULT", {"epic": "DE40", "strategy": "orb", "resolution": "MINUTE_5",
                                                  "r": 1.2 if i % 2 else -1.05})
    refresh(store, mem)
    ex = " ".join(lessons_of(mem, "ExecutionTrader"))
    assert "US500" in ex and "blocked before sending" in ex and "slippage" in ex
    assert any("DE40" in l and "shadow" in l for l in lessons_of(mem, "HeadOfResearch"))


def test_measurements_never_crowd_out_the_agent_memory(tmp_path):
    store, mem = setup(tmp_path)
    for k in range(10):
        trades(store, f"M{k}", "orb", [1, -1, 1, -1, 1])
    refresh(store, mem)
    assert len(mem.recent("CEO", limit=50)) <= 4
