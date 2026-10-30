"""Gewinn-/Verlust-Aufschluesselung nur aus echten Ereignissen; Befunde erst ab genug Trades."""
from src.firm.bus.event_store import EventStore
from src.firm.reports import performance
from src.firm.reports.daily import DailyReporter


def test_groups_capital_trades_and_names_the_losers(tmp_path):
    s = EventStore(str(tmp_path / "e.db"))
    for i in range(6):
        s.append("TRADING_DESK", "TRADE_RESULT", {"epic": "GOLD", "strategy": "orb", "pnl": -10.0, "r": -1.0})
        s.append("TRADING_DESK", "TRADE_RESULT", {"epic": "EURUSD", "strategy": "smc_tjr", "pnl": 15.0, "r": 1.5})
    s.append("TRADING_DESK", "TRADE_RESULT", {"epic": "OIL", "strategy": "orb", "pnl": None})   # ohne P&L: ignoriert
    r = performance.build(s, days=30)
    by_strat = {g["key"]: g for g in r["capital"]["by_strategy"]}
    assert by_strat["orb"]["n"] == 6 and by_strat["orb"]["pnl"] == -60.0 and by_strat["smc_tjr"]["expectancy_r"] == 1.5
    text = " ".join(r["findings"])
    assert "orb" in text and "GOLD" in text
    md = performance.to_markdown(r)
    assert "| orb | 6 |" in md and "kein Versprechen" in md


def test_few_trades_give_no_findings_and_empty_store_works(tmp_path):
    s = EventStore(str(tmp_path / "e.db"))
    r = performance.build(s)
    assert r["capital"]["pnl"] is None
    assert "zu wenige" in r["findings"][0]


def test_daily_report_contains_the_performance_section(tmp_path):
    s = EventStore(str(tmp_path / "e.db"))
    rep = DailyReporter(s, tmp_path / "rep")
    md = rep.to_markdown(rep.generate_report())
    assert "Wo verdient / verliert die Firma Geld?" in md
