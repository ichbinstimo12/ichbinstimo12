import pytest

from src.firm.bus.event_store import EventStore
from src.firm.money.reconciler import Reconciler


class FakeBroker:
    def __init__(self, positions=None):
        self.positions = list(positions or [])

    async def get_positions(self):
        return list(self.positions)


def fill(store, epic, side, size):
    store.append("EXECUTION_GATEWAY", "ORDER_FILLED", {"epic": epic, "side": side, "size": size})


def close(store, epic, direction, size):
    store.append("TRADING_DESK", "POSITION_CLOSED",
                 {"epic": epic, "direction": direction, "size": size, "reason": "test"})


@pytest.fixture
def store(tmp_path):
    return EventStore(str(tmp_path / "events.db"))


def test_internal_book_adds_and_removes(store):
    fill(store, "BTCUSD", "BUY", 1.0)
    reconciler = Reconciler(FakeBroker(), store)

    assert reconciler._calculate_internal_positions() == {"BTCUSD": pytest.approx(1.0)}

    close(store, "BTCUSD", "BUY", 1.0)
    assert reconciler._calculate_internal_positions() == {}


def test_short_position_is_negative(store):
    fill(store, "BTCUSD", "SELL", 0.5)
    reconciler = Reconciler(FakeBroker(), store)

    assert reconciler._calculate_internal_positions()["BTCUSD"] == pytest.approx(-0.5)


@pytest.mark.asyncio
async def test_matching_positions_are_not_critical(store):
    fill(store, "BTCUSD", "SELL", 0.5)
    broker = FakeBroker([{"epic": "BTCUSD", "direction": "SELL", "volume": 0.5}])
    reconciler = Reconciler(broker, store)

    called = []

    async def on_mismatch():
        called.append(True)

    critical = await reconciler.reconcile_once(on_mismatch)

    assert critical is False
    assert called == []


@pytest.mark.asyncio
async def test_broker_side_close_is_booked_out_not_critical(store):
    """A stop-loss / take-profit hit at the broker must not kill the firm."""
    fill(store, "BTCUSD", "BUY", 0.5)
    broker = FakeBroker([])
    reconciler = Reconciler(broker, store)

    called = []

    async def on_mismatch():
        called.append(True)

    critical = await reconciler.reconcile_once(on_mismatch)

    assert critical is False
    assert called == []
    assert reconciler._calculate_internal_positions() == {}
    assert any(e["type"] == "POSITION_CLOSED" for e in store.get_events())


@pytest.mark.asyncio
async def test_unauthorised_exposure_is_critical(store):
    broker = FakeBroker([{"epic": "EURUSD", "direction": "BUY", "volume": 100}])
    reconciler = Reconciler(broker, store)

    called = []

    async def on_mismatch():
        called.append(True)

    critical = await reconciler.reconcile_once(on_mismatch)

    assert critical is True
    assert called == [True]


@pytest.mark.asyncio
async def test_opposite_sides_are_critical(store):
    fill(store, "BTCUSD", "BUY", 0.5)
    broker = FakeBroker([{"epic": "BTCUSD", "direction": "SELL", "volume": 0.5}])
    reconciler = Reconciler(broker, store)

    async def on_mismatch():
        pass

    assert await reconciler.reconcile_once(on_mismatch) is True
