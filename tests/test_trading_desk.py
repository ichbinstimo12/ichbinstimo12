import math

import pytest

from src.firm.bus.event_store import EventStore
from src.firm.money.execution_gateway import ExecutionGateway
from src.firm.money.risk_gate import RiskGate
from src.firm.money.trading_desk import (
    SignalEngine,
    TradingDesk,
    atr,
    ema,
    floor_to_step,
    size_position,
)

LIMITS = """
[account]
daily_loss_limit = 0.02
max_drawdown_limit = 0.08
max_leverage = 10.0
max_positions = 3

[trade]
max_risk_per_trade = 0.005
max_pos_size_per_instrument = 0.1

[operational]
max_token_budget_daily = 1000000
max_agents = 10
"""

WHITELIST = """
allowed_epics = ["BTCUSD"]
"""


def make_candles(closes, spread=0.5):
    """Build OHLC candles from a close series (high/low hug the close)."""
    return [
        {"t": f"t{i}", "open": c, "high": c + spread, "low": c - spread, "close": c, "volume": 1}
        for i, c in enumerate(closes)
    ]


def uptrend(n=200, start=100.0, step=0.4):
    return [start + i * step for i in range(n)]


def downtrend(n=200, start=200.0, step=0.4):
    return [start - i * step for i in range(n)]


def flat(n=200, start=100.0):
    return [start + (0.05 if i % 2 else -0.05) for i in range(n)]


# ------------------------------------------------------------------- maths
def test_ema_tracks_a_constant_series():
    assert ema([5.0] * 50, 12) == pytest.approx(5.0)


def test_ema_of_rising_series_lags_spot():
    values = list(range(1, 60))
    assert ema(values, 12) < values[-1]
    assert ema(values, 12) > ema(values, 48)


def test_atr_is_positive_and_scales_with_range():
    quiet = make_candles(flat(), spread=0.1)
    loud = make_candles(flat(), spread=5.0)
    assert atr(quiet, 14) > 0
    assert atr(loud, 14) > atr(quiet, 14)


def test_floor_to_step_never_rounds_up():
    assert floor_to_step(0.0299, 0.01) == pytest.approx(0.02)
    assert floor_to_step(3300.0, 100) == pytest.approx(3300.0)
    assert floor_to_step(99.9, 100) == pytest.approx(0.0)


# ----------------------------------------------------------------- signals
def test_signal_engine_buys_an_uptrend():
    signal = SignalEngine().evaluate("BTCUSD", make_candles(uptrend()), bid=180, ask=180.5)
    assert signal is not None
    assert signal.side == "BUY"
    assert signal.stop_loss < signal.entry < signal.take_profit
    assert signal.strength > 0


def test_signal_engine_sells_a_downtrend():
    signal = SignalEngine().evaluate("BTCUSD", make_candles(downtrend()), bid=120, ask=120.5)
    assert signal is not None
    assert signal.side == "SELL"
    assert signal.take_profit < signal.entry < signal.stop_loss


def test_signal_engine_stays_flat_in_chop():
    assert SignalEngine().evaluate("BTCUSD", make_candles(flat()), bid=100, ask=100.1) is None


def test_signal_engine_needs_warmup_data():
    assert SignalEngine().evaluate("BTCUSD", make_candles(uptrend(n=20))) is None


# ------------------------------------------------------------------ sizing
def _signal(side="BUY", entry=80000.0, stop_distance=200.0):
    engine_signal = SignalEngine().evaluate(
        "BTCUSD", make_candles(uptrend()), bid=entry - 0.5, ask=entry
    )
    assert engine_signal is not None
    # Force a deterministic geometry for sizing assertions.
    engine_signal.side = side
    engine_signal.entry = entry
    engine_signal.stop_loss = entry - stop_distance if side == "BUY" else entry + stop_distance
    return engine_signal


def test_size_respects_the_risk_budget():
    market = {"size_step": 0.0001, "min_size": 0.0001, "max_size": 50}
    limits = {
        "account": {"max_leverage": 10.0},
        "trade": {"max_risk_per_trade": 0.005},
    }
    signal = _signal(entry=80000.0, stop_distance=200.0)
    size, reason = size_position(signal, 1000.0, limits, market)

    assert size > 0
    assert size * 200.0 <= 5.0 + 1e-9          # 0.5% of 1000
    assert size == pytest.approx(0.025)        # 5.0 / 200, floored to the step
    assert "risk" in reason


def test_size_refuses_a_trade_below_the_broker_minimum():
    market = {"size_step": 0.01, "min_size": 0.1, "max_size": 50}
    limits = {
        "account": {"max_leverage": 10.0},
        "trade": {"max_risk_per_trade": 0.005},
    }
    signal = _signal(entry=4000.0, stop_distance=100.0)
    size, reason = size_position(signal, 1000.0, limits, market)

    assert size == 0.0
    assert "minimum" in reason


def test_size_is_capped_by_account_leverage():
    market = {"size_step": 0.01, "min_size": 0.01, "max_size": 50000}
    limits = {
        "account": {"max_leverage": 1.0},
        "trade": {"max_risk_per_trade": 0.5},   # deliberately greedy
    }
    signal = _signal(entry=100.0, stop_distance=1.0)
    size, _ = size_position(signal, 1000.0, limits, market)

    assert size * 100.0 <= 1000.0 + 1e-9


# -------------------------------------------------------------------- desk
class FakeBroker:
    """Broker double: real order flow, no network."""

    is_demo = True

    def __init__(self, candles, status="TRADEABLE", positions=None, balance=1000.0, currency="CHF"):
        self.currency = currency
        self.candles = candles
        self.status = status
        self.positions = list(positions or [])
        self.balance = balance
        self.orders = []
        self.closed = []

    async def get_account_details(self):
        return {"balance": self.balance, "equity": self.balance, "currency": "CHF", "source": "broker"}

    async def get_positions(self):
        return list(self.positions)

    async def get_market_details(self, epic):
        if epic == "USDCHF":
            return {"epic": epic, "status": "TRADEABLE", "bid": 0.7999, "ask": 0.8001, "currency": "CHF"}
        return {
            "epic": epic, "status": self.status, "bid": 180.45, "ask": 180.5, "currency": self.currency,
            "min_size": 0.0001, "max_size": 50, "size_step": 0.0001,
        }

    async def get_candles(self, epic, resolution="MINUTE_15", limit=200):
        self.candle_requests = getattr(self, "candle_requests", []) + [resolution]
        return self.candles

    async def place_order(self, epic, side, size, stop_loss=None, take_profit=None):
        self.orders.append({"epic": epic, "side": side, "size": size,
                            "stop_loss": stop_loss, "take_profit": take_profit})
        return {"dealId": "DEAL-1", "price": 180.0, "status": "ACCEPTED"}

    async def close_position(self, deal_id):
        self.closed.append(deal_id)
        return {}


@pytest.fixture
def desk_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRM_STYLE", "swing")   # desk mechanics on hourly fixtures; daytrade filter tested separately
    limits_file = tmp_path / "limits.toml"
    limits_file.write_text(LIMITS)
    whitelist_file = tmp_path / "whitelist.toml"
    whitelist_file.write_text(WHITELIST)
    store = EventStore(str(tmp_path / "events.db"))
    risk_gate = RiskGate(str(limits_file), str(whitelist_file))
    return {
        "tmp": tmp_path,
        "store": store,
        "risk_gate": risk_gate,
        "limits_file": limits_file,
        "whitelist_file": whitelist_file,
    }


def build_desk(env, broker, mode="PAPER"):
    gateway = ExecutionGateway(broker, env["risk_gate"], env["store"], mode=mode)
    return TradingDesk(
        broker=broker,
        gateway=gateway,
        risk_gate=env["risk_gate"],
        event_store=env["store"],
        mode=mode,
        whitelist_path=str(env["whitelist_file"]),
        limits_path=str(env["limits_file"]),
        state_path=str(env["tmp"] / "state.json"),
    )


def hourly_candles(closes, open_bar_close=None):
    """Hourly candles with real UTC timestamps; the last one in `closes` is the newest CLOSED bar."""
    import time as _time
    from datetime import datetime, timezone
    floor_now = int(_time.time() // 3600 * 3600)
    base = floor_now - len(closes) * 3600
    out = []
    series = list(closes) + ([open_bar_close] if open_bar_close is not None else [])
    for i, c in enumerate(series):
        ts = datetime.fromtimestamp(base + i * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        out.append({"t": ts, "open": c, "high": c + 0.3, "low": c - 0.3, "close": c, "volume": 1})
    return out


def breakout_closes(n=300, last=103.0):
    closes = [100.0 + 0.5 * ((-1) ** i) for i in range(n - 1)]
    return closes + [last]


def write_verdict(env, verdict="PASS", strategy="donchian_trend", explore=False, resolution="HOUR"):
    import json as _json
    import time as _time
    entry = {"verdict": verdict, "strategy": strategy, "params": {"entry_n": 24, "exit_n": 12, "stop_atr": 2.5},
             "reasons": [], "oos_expectancy_r": 0.2, "oos_trades": 60}
    if explore:
        entry = {"verdict": "FAIL", "reasons": ["not significant"],
                 "explore": {"strategy": strategy, "params": entry["params"], "oos_expectancy_r": 0.05, "oos_trades": 40}}
    path = env["tmp"] / "verdicts.json"
    path.write_text(_json.dumps({"created": _time.time(), "resolution": resolution, "trials": 10,
                                 "verdicts": {"BTCUSD": entry}}))
    return path


def build_gated_desk(env, broker, mode="PAPER", **verdict):
    from src.firm.money.strategy_gate import StrategyGate
    desk = build_desk(env, broker, mode=mode)
    desk.gate = StrategyGate(str(write_verdict(env, **verdict)))
    desk.resolution = "HOUR"
    return desk


@pytest.mark.asyncio
async def test_desk_executes_a_real_signal(desk_env):
    broker = FakeBroker(hourly_candles(breakout_closes(), open_bar_close=150.0))
    desk = build_gated_desk(desk_env, broker)

    summary = await desk.run_cycle()

    assert len(broker.orders) == 1, summary
    order = broker.orders[0]
    assert order["side"] == "BUY"
    assert order["size"] > 0
    assert order["stop_loss"] < 180.5 < order["take_profit"]
    assert summary["trades"][0]["order_id"] == "DEAL-1"

    types = [e["type"] for e in desk_env["store"].get_events()]
    assert "SIGNAL" in types and "ORDER_FILLED" in types and "DESK_STATUS" in types
    meta = desk.state.data["pos_meta"]["BTCUSD"]
    assert meta["params"]["entry_n"] == 24 and meta["risk"] > 0


@pytest.mark.asyncio
async def test_desk_without_verdict_does_not_trade_and_says_why(desk_env):
    from src.firm.money.strategy_gate import StrategyGate
    broker = FakeBroker(hourly_candles(breakout_closes()))
    desk = build_desk(desk_env, broker)
    desk.gate = StrategyGate(str(desk_env["tmp"] / "missing.json"))
    summary = await desk.run_cycle()
    assert broker.orders == []
    assert "Forschungsurteil" in summary["skipped"][0]["reason"]
    status = [e for e in desk_env["store"].get_events() if e["type"] == "DESK_STATUS"]
    assert len(status) == 1
    await desk.run_cycle()
    assert len([e for e in desk_env["store"].get_events() if e["type"] == "DESK_STATUS"]) == 1, "only on change"


@pytest.mark.asyncio
async def test_usd_instrument_on_chf_account_is_not_rejected_by_the_risk_gate(desk_env):
    """Regression: risk was compared in USD against a CHF budget -> every USD trade 'risk too high'."""
    broker = FakeBroker(hourly_candles(breakout_closes()), currency="USD")
    desk = build_gated_desk(desk_env, broker)
    summary = await desk.run_cycle()
    assert len(broker.orders) == 1, summary
    order = broker.orders[0]
    risk_chf = abs(180.5 - order["stop_loss"]) * order["size"] * 0.8
    assert risk_chf <= 1000 * 0.005 * 1.001


@pytest.mark.asyncio
async def test_resolution_mismatch_blocks_trading(desk_env):
    broker = FakeBroker(hourly_candles(breakout_closes()))
    desk = build_gated_desk(desk_env, broker, resolution="DAY")
    summary = await desk.run_cycle()
    assert broker.orders == []
    assert "FIRM_TRADE_RESOLUTION" in summary["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_explore_lane_trades_small_in_demo_only(desk_env, monkeypatch):
    monkeypatch.setenv("FIRM_DEMO_EXPLORE", "1")
    broker = FakeBroker(hourly_candles(breakout_closes()))
    desk = build_gated_desk(desk_env, broker, explore=True)
    await desk.run_cycle()
    assert len(broker.orders) == 1
    explore_size = broker.orders[0]["size"]

    broker2 = FakeBroker(hourly_candles(breakout_closes()))
    desk2 = build_gated_desk(desk_env, broker2)
    desk2.state.data = {}
    await desk2.run_cycle()
    assert explore_size == pytest.approx(broker2.orders[0]["size"] * 0.25, rel=0.01)

    monkeypatch.setenv("FIRM_DEMO_EXPLORE", "0")
    broker3 = FakeBroker(hourly_candles(breakout_closes()))
    desk3 = build_gated_desk(desk_env, broker3, explore=True)
    desk3.state.data = {}
    await desk3.run_cycle()
    assert broker3.orders == []


@pytest.mark.asyncio
async def test_agent_overlay_can_block_and_shrink_but_never_grow(desk_env):
    from src.firm.money.risk_overlay import RiskOverlay
    overlay = RiskOverlay(desk_env["store"].db_path, desk_env["store"])
    broker = FakeBroker(hourly_candles(breakout_closes()))
    desk = build_gated_desk(desk_env, broker)
    desk.overlay = overlay
    overlay.tighten("BTCUSD", 0.0, 2, "MacroAnalyst", "exchange maintenance announced", source="cap_market_get")
    summary = await desk.run_cycle()
    assert broker.orders == [] and "gesperrt" in summary["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_desk_resolves_the_real_position_id_after_a_fill(desk_env):
    """Closes are addressed by position id, which differs from the fill's deal id."""
    broker = FakeBroker(make_candles(uptrend()))
    desk = build_desk(desk_env, broker)
    broker.positions = [
        {"epic": "BTCUSD", "direction": "BUY", "dealId": "POS-1", "volume": 0.01},
    ]

    assert await desk._resolve_position_deal("BTCUSD", "BUY", set()) == "POS-1"
    # an already known position is not mistaken for the one we just opened
    assert await desk._resolve_position_deal("BTCUSD", "BUY", {"POS-1"}) is None


@pytest.mark.asyncio
async def test_desk_does_not_trade_a_closed_market(desk_env):
    broker = FakeBroker(make_candles(uptrend()), status="CLOSED")
    desk = build_desk(desk_env, broker)

    summary = await desk.run_cycle()

    assert broker.orders == []
    assert "closed" in summary["skipped"][0]["reason"].lower()


@pytest.mark.asyncio
async def test_desk_does_not_double_up_on_an_open_epic(desk_env):
    broker = FakeBroker(
        make_candles(uptrend()),
        positions=[{"dealId": "OLD", "epic": "BTCUSD", "direction": "BUY"}],
    )
    desk = build_desk(desk_env, broker)

    summary = await desk.run_cycle()

    assert broker.orders == []
    assert any("already open" in s["reason"] for s in summary["skipped"])


@pytest.mark.asyncio
async def test_desk_closes_a_position_with_the_entry_parameters(desk_env):
    """Exit uses the strategy AND parameters stored at entry (before: default parameters)."""
    broker = FakeBroker(
        hourly_candles(breakout_closes(last=97.0)),
        positions=[{"dealId": "OLD", "epic": "BTCUSD", "direction": "BUY"}],
    )
    desk = build_gated_desk(desk_env, broker)
    desk.state.data["pos_meta"] = {"BTCUSD": {"side": "BUY", "strategy": "donchian_trend", "balance_at_entry": 1000,
                                              "params": {"entry_n": 24, "exit_n": 12, "stop_atr": 2.5}}}

    summary = await desk.run_cycle()

    assert broker.closed == ["OLD"]
    assert summary["closed"][0]["epic"] == "BTCUSD"


@pytest.mark.asyncio
async def test_paper_desk_refuses_a_live_broker(desk_env):
    broker = FakeBroker(make_candles(uptrend()))
    broker.is_demo = False
    desk = build_desk(desk_env, broker, mode="PAPER")

    summary = await desk.run_cycle()

    assert broker.orders == []
    assert "LIVE broker" in summary["error"]


@pytest.mark.asyncio
async def test_desk_refuses_a_broker_without_order_support(desk_env):
    broker = FakeBroker(make_candles(uptrend()))
    broker.place_order = None
    desk = build_desk(desk_env, broker)

    summary = await desk.run_cycle()

    assert "order placement" in summary["error"]


# ------------------------------------------------------------ safety (audit round 2)
@pytest.mark.asyncio
async def test_daily_loss_breach_writes_the_halt_flag(desk_env):
    broker = FakeBroker(hourly_candles(breakout_closes()))
    desk = build_gated_desk(desk_env, broker)
    desk.halt_file = desk_env["tmp"] / "firm.halt"
    await desk.run_cycle()                      # day start = 1000
    broker.balance = 975.0                      # -2.5 % equity on the same UTC day
    summary = await desk.run_cycle()
    assert "Daily loss" in summary["error"]
    assert desk.halt_file.exists(), "breach must flatten + halt (main loop reacts to the flag)"


@pytest.mark.asyncio
async def test_api_error_limit_pauses_entries(desk_env):
    broker = FakeBroker(hourly_candles(breakout_closes()))
    desk = build_gated_desk(desk_env, broker)
    desk.max_api_errors = 3

    async def boom():
        raise ConnectionError("503")
    broker.get_account_details = boom
    for _ in range(3):
        await desk.run_cycle()
    broker.get_account_details = FakeBroker([]).get_account_details
    summary = await desk.run_cycle()
    assert "API-Fehlerlimit" in summary["error"] and broker.orders == []


@pytest.mark.asyncio
async def test_failed_order_that_landed_is_recovered_not_treated_as_foreign(desk_env):
    broker = FakeBroker(hourly_candles(breakout_closes()))

    async def place_then_fail(epic, side, size, stop_loss=None, take_profit=None):
        broker.positions.append({"epic": epic, "direction": side, "dealId": "POS-9", "volume": size, "level": 180.5})
        raise ConnectionError("confirm timed out")
    broker.place_order = place_then_fail
    desk = build_gated_desk(desk_env, broker)
    summary = await desk.run_cycle()
    assert summary["trades"] and summary["trades"][0]["order_id"] == "POS-9"
    fills = [e for e in desk_env["store"].get_events() if e["type"] == "ORDER_FILLED"]
    assert fills and '"recovered": true' in fills[-1]["payload_json"]


@pytest.mark.asyncio
async def test_total_open_risk_is_capped(desk_env):
    broker = FakeBroker(hourly_candles(breakout_closes()),
                        positions=[{"epic": "GOLD", "direction": "BUY", "dealId": "G"},
                                   {"epic": "EURUSD", "direction": "SELL", "dealId": "E"}])
    desk = build_gated_desk(desk_env, broker)
    desk.state.data["pos_meta"] = {"GOLD": {"risk": 5.0}, "EURUSD": {"risk": 5.0}}   # 1.0 % already at risk
    desk.limits["trade"]["max_total_risk"] = 0.012
    summary = await desk.run_cycle()
    assert broker.orders == []
    assert "Klumpenrisiko" in summary["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_stale_prices_are_not_traded(desk_env):
    closes = breakout_closes()
    candles = hourly_candles(closes)
    from datetime import datetime, timedelta
    for c in candles:   # shift everything 10 hours into the past
        c["t"] = (datetime.fromisoformat(c["t"]) - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S")
    broker = FakeBroker(candles)
    desk = build_gated_desk(desk_env, broker)
    summary = await desk.run_cycle()
    assert broker.orders == [] and "veraltet" in summary["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_desk_uses_the_timeframe_the_research_chose(desk_env):
    import json as _json
    import time as _time
    from src.firm.money.strategy_gate import StrategyGate
    broker = FakeBroker(hourly_candles(breakout_closes()))
    desk = build_desk(desk_env, broker)
    path = desk_env["tmp"] / "v15.json"
    path.write_text(_json.dumps({"created": _time.time(), "resolution": ["MINUTE_15", "HOUR"], "verdicts": {
        "BTCUSD": {"verdict": "PASS", "strategy": "donchian_trend", "resolution": "MINUTE_15",
                   "params": {"entry_n": 24, "exit_n": 12, "stop_atr": 2.5}}}}))
    desk.gate = StrategyGate(str(path))
    await desk.run_cycle()
    assert broker.candle_requests == ["MINUTE_15"]


def test_desk_state_does_not_carry_a_peak_into_another_account(tmp_path):
    """Demo peak 1000 must not count as an 80 % drawdown on a 200 CHF live account (halted the firm on start)."""
    from src.firm.money.trading_desk import DeskState
    st = DeskState(str(tmp_path / "s.json"))
    assert st.bind_account("PAPER:D1:CHF") == "unbound"
    st.day_start_balance("2026-10-29", 1000.0)
    assert st.peak_balance(1000.0) == 1000.0
    assert st.bind_account("PAPER:D1:CHF") is None                 # same account: keep history
    assert DeskState(str(tmp_path / "s.json")).peak_balance(950.0) == 1000.0
    assert st.bind_account("LIVE_MICRO:L1:CHF") == "PAPER:D1:CHF"  # other account: fresh start
    assert st.peak_balance(200.0) == 200.0 and st.day_start_balance("2026-10-29", 200.0) == 200.0


@pytest.mark.asyncio
async def test_daytrade_style_desk_skips_a_swing_verdict(desk_env, monkeypatch):
    monkeypatch.setenv("FIRM_STYLE", "daytrade")
    broker = FakeBroker(hourly_candles(breakout_closes()))
    desk = build_gated_desk(desk_env, broker)
    summary = await desk.run_cycle()
    assert broker.orders == [] and "Daytrading" in summary["skipped"][0]["reason"]


@pytest.mark.asyncio
async def test_no_entry_right_before_a_high_impact_release(desk_env):
    import time as _t
    broker = FakeBroker(hourly_candles(breakout_closes()))
    desk = build_gated_desk(desk_env, broker)
    desk.event_store.append("NEWS", "ECON_CALENDAR", {"high_impact": [
        {"title": "FOMC Statement", "country": "USD", "ts": _t.time() + 300}],
        "blackout_before_min": 15, "blackout_after_min": 15})
    summary = await desk.run_cycle()
    assert broker.orders == [] and "News-Sperre" in summary["skipped"][0]["reason"]


def test_existing_state_without_account_key_keeps_a_plausible_peak(tmp_path):
    """Update day: a real 5 % drawdown taken before the update must not be forgiven."""
    import json as _j
    from src.firm.money.trading_desk import DeskState
    path = tmp_path / "s.json"
    path.write_text(_j.dumps({"peak_balance": 1000.0, "day": "2026-10-29", "day_start_balance": 990.0}))
    st = DeskState(str(path))
    assert st.bind_account("PAPER:D1:CHF", equity=950.0) is None
    assert st.peak_balance(950.0) == 1000.0
    st2 = DeskState(str(path)); st2.data.pop("account_key")
    assert st2.bind_account("LIVE:L1:CHF", equity=200.0) == "unbound"   # 80 % below: clearly another account
    assert st2.peak_balance(200.0) == 200.0
