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

    def __init__(self, candles, status="TRADEABLE", positions=None, balance=1000.0):
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
        return {
            "epic": epic, "status": self.status, "bid": 180.0, "ask": 180.5,
            "min_size": 0.0001, "max_size": 50, "size_step": 0.0001,
        }

    async def get_candles(self, epic, resolution="MINUTE_15", limit=200):
        return self.candles

    async def place_order(self, epic, side, size, stop_loss=None, take_profit=None):
        self.orders.append({"epic": epic, "side": side, "size": size,
                            "stop_loss": stop_loss, "take_profit": take_profit})
        return {"dealId": "DEAL-1", "price": 180.0, "status": "ACCEPTED"}

    async def close_position(self, deal_id):
        self.closed.append(deal_id)
        return {}


@pytest.fixture
def desk_env(tmp_path):
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


@pytest.mark.asyncio
async def test_desk_executes_a_real_signal(desk_env):
    broker = FakeBroker(make_candles(uptrend()))
    desk = build_desk(desk_env, broker)

    summary = await desk.run_cycle()

    assert len(broker.orders) == 1
    order = broker.orders[0]
    assert order["side"] == "BUY"
    assert order["size"] > 0
    assert order["stop_loss"] < 180.5 < order["take_profit"]
    assert summary["trades"][0]["order_id"] == "DEAL-1"

    types = [e["type"] for e in desk_env["store"].get_events()]
    assert "SIGNAL" in types
    assert "ORDER_FILLED" in types


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
async def test_desk_closes_a_position_when_the_trend_flips(desk_env):
    broker = FakeBroker(
        make_candles(downtrend()),
        positions=[{"dealId": "OLD", "epic": "BTCUSD", "direction": "BUY"}],
    )
    desk = build_desk(desk_env, broker)

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
