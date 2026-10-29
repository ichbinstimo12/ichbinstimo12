"""Daytrading: Handelsfenster, Glattstellung vor Sitzungsende, kein Halten ueber Nacht, Zeitrahmen je Markt."""
import copy
import json
import time

import pytest

from src.firm.money.signal_v2 import SignalEngineV2
from src.firm.money.strategy_gate import StrategyGate
from src.firm.research import lab, pipeline
from tests.synth import make

SESSION = dict(intraday=True, entry_start_h=7.0, entry_end_h=19.0, flat_h=20.0)
DAY0 = 1_700_006_400 - (1_700_006_400 % 86400)  # UTC midnight


def m15(n=6000, seed=3, **kw):
    return make(n, seed=seed, regime=True, step=900, t0=DAY0, **kw)


@pytest.mark.parametrize("name", lab.DAYTRADE_SET)
def test_intraday_candidates_never_hold_overnight_and_respect_the_window(name):
    cs = m15()
    trades = lab.backtest(cs, lab.REGISTRY[name](**SESSION), overnight_pct_day=0.001, bar_seconds=900)
    for t in trades:
        entry_ts, exit_ts = cs[t["entry_i"]]["ts"], cs[t["exit_i"]]["ts"]
        assert lab.day_utc(entry_ts) == lab.day_utc(exit_ts), "position held over night"
        assert 7.0 <= lab.hour_utc(entry_ts) < 19.0
        assert lab.hour_utc(exit_ts) <= 20.0 + 0.25


@pytest.mark.parametrize("name", lab.INTRADAY_ONLY)
def test_intraday_strategies_have_no_lookahead(name):
    cs = m15(2000, seed=9)
    s = lab.REGISTRY[name](**SESSION)
    a = lab.Ind(cs)
    ref = [s.entry(a, i) for i in range(len(cs))]
    mut = copy.deepcopy(cs)
    for c in mut[1500:]:
        c["c"] *= 1.3; c["h"] *= 1.4; c["l"] *= 0.7; c["o"] *= 1.1
    b = lab.Ind(mut)
    assert ref[:1400] == [s.entry(b, i) for i in range(1400)]


def test_daytrading_pays_no_overnight_financing():
    cs = m15()
    s = lab.REGISTRY["donchian_trend"](**SESSION)
    free = lab.metrics(lab.backtest(cs, s, bar_seconds=900))["expectancy_r"]
    fee = lab.metrics(lab.backtest(cs, s, overnight_pct_day=0.01, bar_seconds=900))["expectancy_r"]
    assert free == pytest.approx(fee)


def test_live_engine_flattens_before_close_and_blocks_entries_outside_window():
    now = [DAY0 + 20.5 * 3600]
    eng = SignalEngineV2("orb", SESSION, resolution="MINUTE_15", now_fn=lambda: now[0])
    assert eng.should_exit("X", [], "BUY", opened_ts=DAY0 + 10 * 3600) is True
    now[0] = DAY0 + 12 * 3600
    assert eng.should_exit("X", [], "BUY", opened_ts=DAY0 + 10 * 3600) is False
    now[0] = DAY0 + 22 * 3600
    cs = [{"t": None, "open": 1, "high": 1, "low": 1, "close": 1}]
    assert eng.evaluate("X", cs) is None
    assert not eng.strat.can_enter_at(now[0])


def test_multi_timeframe_research_records_the_winning_resolution(tmp_path):
    data = {"MINUTE_15": {"TREND": make(5000, seed=201, regime=True, mu=0.0008, vol=0.0015, step=900, t0=DAY0)},
            "HOUR": {"TREND": make(4500, seed=201, regime=True, mu=0.0012, vol=0.002, t0=DAY0)}}
    verdicts, lines, trials = pipeline.run_research_multi(data, log=lambda *_: None)
    text = "\n".join(lines)
    assert "MINUTE_15 (Daytrading" in text and "HOUR (Swing" in text
    v = verdicts["TREND"]
    if v.get("strategy"):
        assert v["resolution"] in ("MINUTE_15", "HOUR")
        if v["resolution"] == "MINUTE_15":
            assert v["params"]["intraday"] is True and "flat_h" in v["params"]


def test_gate_returns_the_per_market_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRM_STYLE", "swing")
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"created": time.time(), "resolution": ["MINUTE_15", "HOUR"], "verdicts": {
        "EURUSD": {"verdict": "PASS", "strategy": "orb", "params": SESSION, "resolution": "MINUTE_15"},
        "GOLD": {"verdict": "PASS", "strategy": "donchian_trend", "params": {}, "resolution": "HOUR"}}}))
    gate = StrategyGate(str(path))
    assert gate.decide("EURUSD", True, "PAPER", "HOUR")["resolution"] == "MINUTE_15"
    assert gate.decide("GOLD", True, "PAPER", "HOUR")["resolution"] == "HOUR"


def test_sessions_follow_daylight_saving_time():
    """24/7-Betrieb: New-York-Sitzung beginnt im Winter 14:30 UTC, im Sommer 13:30 UTC."""
    from datetime import datetime, timezone
    s = lab.REGISTRY["orb"](intraday=True, entry_start_h=9.5, entry_end_h=14.5, flat_h=15.75, session_tz="America/New_York")
    winter = datetime(2026, 1, 15, 14, 35, tzinfo=timezone.utc).timestamp()
    summer = datetime(2026, 7, 15, 13, 35, tzinfo=timezone.utc).timestamp()
    assert s.can_enter_at(winter) and s.can_enter_at(summer)
    assert not s.can_enter_at(datetime(2026, 7, 15, 12, 35, tzinfo=timezone.utc).timestamp())   # 08:35 NY
    assert s.must_flat(summer, datetime(2026, 7, 15, 19, 50, tzinfo=timezone.utc).timestamp())   # 15:50 NY
    assert not s.must_flat(summer, datetime(2026, 7, 15, 19, 40, tzinfo=timezone.utc).timestamp())


def test_session_config_includes_timezone():
    base = pipeline.load_session("AAPL")
    assert base["session_tz"] == "America/New_York" and base["intraday"] is True


def test_daytrade_style_blocks_swing_verdicts_and_ignores_swing_timeframes(tmp_path, monkeypatch):
    from src.firm.main import research_resolutions
    monkeypatch.delenv("FIRM_STYLE", raising=False)
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"created": time.time(), "resolution": ["MINUTE_15", "HOUR"], "verdicts": {
        "EURUSD": {"verdict": "PASS", "strategy": "orb", "params": SESSION, "resolution": "MINUTE_15"},
        "GOLD": {"verdict": "PASS", "strategy": "donchian_trend", "params": {}, "resolution": "HOUR"}}}))
    gate = StrategyGate(str(path))
    assert gate.decide("EURUSD", True, "PAPER", "MINUTE_15")["ok"]
    gold = gate.decide("GOLD", True, "PAPER", "MINUTE_15")
    assert not gold["ok"] and "Daytrading" in gold["reason"]
    monkeypatch.setenv("FIRM_RESEARCH_RESOLUTIONS", "MINUTE_15,HOUR")      # old .env
    assert research_resolutions("MINUTE_15") == ["MINUTE_15"]
    monkeypatch.delenv("FIRM_RESEARCH_RESOLUTIONS")
    assert research_resolutions("MINUTE_15") == ["MINUTE_15", "MINUTE_5"]
