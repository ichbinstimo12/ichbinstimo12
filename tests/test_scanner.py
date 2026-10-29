"""Markt-Scanner: ganze Capital.com-Marktliste -> Kandidaten nach Kosten/Bewegung, Sekundentakt, Treffer als Ereignisse."""
import asyncio
import json

import pytest

from src.firm.bus.event_store import EventStore
from src.firm.money.scanner import MarketScanner, pick_universe
from tests.test_smc_tjr import STEP, bullish_day


def m(epic, bid, offer, change, status="TRADEABLE"):
    return {"epic": epic, "bid": bid, "offer": offer, "percentageChange": change, "marketStatus": status}


def test_universe_prefers_cheap_and_moving_markets_and_skips_closed_or_expensive():
    markets = [m("CHEAP_MOVER", 100, 100.02, 2.0), m("CHEAP_FLAT", 100, 100.02, 0.1),
               m("EXPENSIVE", 100, 101, 5.0), m("CLOSED", 100, 100.01, 3.0, status="CLOSED"),
               m("NOPRICE", None, None, 1.0)]
    picked = pick_universe(markets, exclude=["EURUSD"], limit=5, max_spread_pct=0.1)
    assert [p["epic"] for p in picked] == ["CHEAP_MOVER", "CHEAP_FLAT"]
    assert picked[0]["spread_pct"] == pytest.approx(0.02, rel=1e-3)


class FakeStream:
    def __init__(self):
        self.epics = []

    async def set_epics(self, epics):
        self.epics = list(epics)
        return {}

    def quote(self, epic, max_age=10):
        return {"bid": 100.0, "ask": 100.02, "received": 0}


class FakeBroker:
    def __init__(self, candles):
        self.candles, self.calls = candles, []

    async def get_market_list(self):
        return [m("SMCX", 100, 100.02, 1.5), m("EURUSD", 1.1, 1.1001, 0.2)]

    async def get_candles(self, epic, resolution="MINUTE_5", limit=200):
        self.calls.append(epic)
        return [{"t": c["ts"], "open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"]} for c in self.candles]


def _scanner(tmp_path, candles, now):
    store = EventStore(str(tmp_path / "s.db"))
    broker, stream = FakeBroker(candles), FakeStream()
    sc = MarketScanner(broker, stream, store, whitelist=["EURUSD"], resolution="MINUTE_5", universe_size=10,
                       now_fn=lambda: now[0], session_fn=lambda epic: dict(intraday=True, entry_start_h=7.0,
                                                                        entry_end_h=16.0, flat_h=19.0))
    return sc, store, broker, stream


def test_universe_refresh_subscribes_whitelist_first(tmp_path):
    sc, store, _, stream = _scanner(tmp_path, [], [0.0])
    asyncio.run(sc.refresh_universe())
    assert stream.epics == ["EURUSD", "SMCX"]
    ev = store.get_events(types=["UNIVERSE_UPDATED"])
    assert json.loads(ev[-1]["payload_json"])["markets_total"] == 2


def test_bar_close_triggers_a_scan_and_reports_tjr_setup_and_hit_once(tmp_path):
    cs = bullish_day()[:91]                      # last closed bar = the FVG retrace entry
    close_of_last = cs[-1]["ts"] + STEP
    now = [close_of_last + 3]
    sc, store, broker, _ = _scanner(tmp_path, cs, now)
    asyncio.run(sc.refresh_universe())
    asyncio.run(sc.tick())                       # bar closed 3 s ago -> scan queued + processed
    asyncio.run(sc.tick())
    assert set(broker.calls) == {"EURUSD", "SMCX"}
    hits = [json.loads(e["payload_json"]) for e in store.get_events(types=["SCANNER_HIT"])]
    smc = [h for h in hits if h["strategy"] == "smc_tjr" and h["epic"] == "SMCX"]
    assert len(smc) == 1 and smc[0]["side"] == "BUY" and smc[0]["whitelisted"] is False
    setups = [json.loads(e["payload_json"]) for e in store.get_events(types=["SMC_SETUP"])]
    assert any(s["epic"] == "SMCX" and s["stage"] == "entry" for s in setups)
    n_calls = len(broker.calls)
    now[0] += 1
    asyncio.run(sc.tick())                       # same bar: nothing new
    assert len(broker.calls) == n_calls and len(store.get_events(types=["SCANNER_HIT"])) == len(hits)


@pytest.mark.asyncio
async def test_desk_wakes_shortly_after_the_next_candle_close(monkeypatch):
    from src.firm import main as firm_main
    orch = firm_main.FirmOrchestrator.__new__(firm_main.FirmOrchestrator)
    orch.is_running = True
    orch._desk_resolutions = lambda: {"MINUTE_5"}
    clock = [10_000 * 300 + 299.0]                       # 1 s before a 5-minute close
    monkeypatch.setattr(firm_main.time, "time", lambda: clock[0])

    async def fake_sleep(s):
        clock[0] += s
    monkeypatch.setattr(firm_main.asyncio, "sleep", fake_sleep)
    await orch._sleep_until_next_bar(60)
    assert clock[0] == pytest.approx(10_001 * 300 + 2.0)  # close + 2 s, not 60 s later


def test_shadow_book_tracks_hits_to_target_or_stop_without_trading(tmp_path):
    from tests.test_smc_tjr import bar
    cs = bullish_day()[:91]
    now = [cs[-1]["ts"] + STEP + 3]
    sc, store, broker, _ = _scanner(tmp_path, cs, now)
    asyncio.run(sc.refresh_universe())
    asyncio.run(sc.tick()); asyncio.run(sc.tick())
    smc = [s for s in sc.shadows if s["strategy"] == "smc_tjr" and s["epic"] == "SMCX"]
    assert len(smc) == 1 and smc[0]["target"] > smc[0]["entry"] > smc[0]["stop"]
    target = smc[0]["target"]
    later = cs + [bar(91 + k, 101, 101.5 + (6 if k == 3 else 0), 100.8, 101.2) for k in range(5)]
    later[-2]["h"] = target + 0.5                          # target hit before any stop
    broker.candles = later
    now[0] = later[-1]["ts"] + STEP + 3
    asyncio.run(sc.tick()); asyncio.run(sc.tick())
    res = [json.loads(e["payload_json"]) for e in store.get_events(types=["SHADOW_RESULT"])]
    mine = [r for r in res if r["strategy"] == "smc_tjr" and r["epic"] == "SMCX"]
    assert len(mine) == 1 and mine[0]["outcome"] == "target" and mine[0]["r"] > 1.5
    assert not any(s["strategy"] == "smc_tjr" and s["epic"] == "SMCX" for s in sc.shadows)


def test_shadow_stop_first_when_both_in_one_bar(tmp_path):
    from src.firm.money.scanner import resolve_shadow
    sh = {"side": "BUY", "entry": 100.0, "stop": 99.0, "target": 102.0, "bar_ts": 0, "cost_r": 0.1, "max_bars": 10}
    bars = [{"ts": 300, "o": 100, "h": 102.5, "l": 98.5, "c": 101}]
    out = resolve_shadow(sh, bars, 300)
    assert out["outcome"] == "stop" and out["r"] == -1.1                    # conservative: stop counts first
