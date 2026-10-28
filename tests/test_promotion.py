"""Skalierungs-Leiter: nur Empfehlung, harte Kriterien, Pruefung des Live-Kapitals."""
import json
import time
from datetime import datetime, timedelta

from src.firm.bus.event_store import EventStore
from src.firm.money.promotion import promotion_check
from src.firm.money.strategy_gate import StrategyGate

LIMITS = {"account": {"max_drawdown_limit": 0.08}, "trade": {"max_risk_per_trade": 0.005}}


def _gate(tmp_path, stop_pct=0.004):
    p = tmp_path / "v.json"
    p.write_text(json.dumps({"created": time.time(), "verdicts": {
        "EURUSD": {"verdict": "PASS", "strategy": "orb", "oos_expectancy_r": 0.2, "median_stop_pct": stop_pct},
        "BTCUSD": {"verdict": "PASS", "strategy": "donchian_trend", "oos_expectancy_r": 0.2, "median_stop_pct": 0.03}}}))
    return StrategyGate(str(p))


def test_fresh_firm_is_not_ready(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    r = promotion_check(store, _gate(tmp_path), LIMITS, "PAPER", live_balance=200,
                        markets={"EURUSD": {"price": 1.08, "min_size": 100, "fx": 0.8},
                                 "BTCUSD": {"price": 67000, "min_size": 0.01, "fx": 0.8}})
    assert r["next"] == "LIVE_MICRO" and r["ready"] is False
    live = r["criteria"][-1]
    assert live["ok"] is True and "BTCUSD: Mindestgroesse riskiert" in live["detail"]


def test_200_chf_cannot_trade_when_minimum_size_is_too_big(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    r = promotion_check(store, _gate(tmp_path, stop_pct=0.05), LIMITS, "PAPER", live_balance=200,
                        markets={"EURUSD": {"price": 1.08, "min_size": 1000, "fx": 0.8},
                                 "BTCUSD": {"price": 67000, "min_size": 0.01, "fx": 0.8}})
    assert r["criteria"][-1]["ok"] is False


def test_all_criteria_met_means_ready_but_never_switches(tmp_path, monkeypatch):
    store = EventStore(str(tmp_path / "e.db"))
    import sqlite3
    old = (datetime.utcnow() - timedelta(days=40)).isoformat()
    with sqlite3.connect(store.db_path) as c:
        c.execute("INSERT INTO events VALUES ('x', ?, 'EXECUTION_GATEWAY', 'ORDER_FILLED', '{}', NULL)", (old,))
    for i in range(60):
        store.append("TRADING_DESK", "TRADE_RESULT", {"r": 0.3 if i % 3 else -1.0 + 1.3})
    for v in (1000, 1010, 1005, 1020):
        store.append("SYSTEM", "ACCOUNT_SNAPSHOT", {"equity": v})
    r = promotion_check(store, _gate(tmp_path), LIMITS, "PAPER", live_balance=1000,
                        markets={"EURUSD": {"price": 1.08, "min_size": 100, "fx": 0.8}})
    assert r["ready"] is True, r
    assert "Empfehlung" in r["note"]
