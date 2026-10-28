"""WebSocket-Kursstream gegen einen lokalen Server, der das Capital.com-Protokoll nachbildet."""
import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from src.firm.broker.stream import PriceStream


async def _session():
    return "CST-1", "TOKEN-1"


@pytest.mark.asyncio
async def test_stream_subscribes_receives_quotes_and_reconnects():
    seen, conns = [], []

    async def handler(ws):
        conns.append(ws)
        msg = json.loads(await ws.recv())
        seen.append(msg)
        await ws.send(json.dumps({"status": "OK", "destination": "marketData.subscribe",
                                  "payload": {"subscriptions": {e: "PROCESSED" for e in msg["payload"]["epics"]}}}))
        await ws.send(json.dumps({"status": "OK", "destination": "quote",
                                  "payload": {"epic": "GOLD", "bid": 2400.1, "ofr": 2400.5, "timestamp": 1730000000000}}))
        if len(conns) == 1:
            await ws.close()          # first connection drops -> client must reconnect
        else:
            await asyncio.sleep(5)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stream = PriceStream(_session, ["GOLD", "EURUSD"], url=f"ws://127.0.0.1:{port}")
        task = asyncio.create_task(stream.run())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(conns) >= 2 and stream.connected:
                break
        q = stream.quote("GOLD", max_age=10)
        await stream.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert seen[0]["destination"] == "marketData.subscribe"
    assert seen[0]["cst"] == "CST-1" and seen[0]["securityToken"] == "TOKEN-1"
    assert seen[0]["payload"]["epics"] == ["GOLD", "EURUSD"]
    assert q == {"bid": 2400.1, "ask": 2400.5, "ts": 1730000000.0, "received": q["received"]}
    assert stream.reconnects >= 1


def test_old_quotes_are_not_used():
    clock = [1000.0]
    s = PriceStream(_session, ["GOLD"], now_fn=lambda: clock[0])
    s._handle(json.dumps({"destination": "quote", "payload": {"epic": "GOLD", "bid": 1, "ofr": 2}}))
    assert s.quote("GOLD", max_age=10)
    clock[0] += 11
    assert s.quote("GOLD", max_age=10) is None
    s._handle("not json")
    s._handle(json.dumps({"destination": "quote", "payload": {"epic": "X"}}))
    assert "X" not in s.quotes


@pytest.mark.asyncio
async def test_desk_rejects_entries_on_a_stale_stream(tmp_path):
    from tests.test_trading_desk import (FakeBroker, breakout_closes, build_gated_desk, hourly_candles, LIMITS,
                                         WHITELIST)
    from src.firm.bus.event_store import EventStore
    from src.firm.money.risk_gate import RiskGate
    (tmp_path / "limits.toml").write_text(LIMITS)
    (tmp_path / "whitelist.toml").write_text(WHITELIST)
    env = {"tmp": tmp_path, "store": EventStore(str(tmp_path / "e.db")),
           "risk_gate": RiskGate(str(tmp_path / "limits.toml"), str(tmp_path / "whitelist.toml")),
           "limits_file": tmp_path / "limits.toml", "whitelist_file": tmp_path / "whitelist.toml"}
    broker = FakeBroker(hourly_candles(breakout_closes()))
    desk = build_gated_desk(env, broker)
    clock = [1000.0]
    stream = PriceStream(_session, ["BTCUSD"], now_fn=lambda: clock[0])
    stream.connected = True
    desk.stream = stream
    summary = await desk.run_cycle()
    assert broker.orders == [] and "Live-Kurs" in summary["skipped"][0]["reason"]
    stream._handle(json.dumps({"destination": "quote", "payload": {"epic": "BTCUSD", "bid": 180.45, "ofr": 180.5}}))
    desk.state.data = {}
    for e in desk._engines.values():
        e._last_bar.clear()
    summary = await desk.run_cycle()
    assert len(broker.orders) == 1, summary
