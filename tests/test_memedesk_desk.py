"""Memecoin-Desk end-to-end im PAPER-Modus mit simulierten Quellen: Kauf, Veto, kein Hinterherrennen,
Free Ride, Kill-Switch, Journal und Ereignisse."""
import asyncio
import json

from src.firm.bus.event_store import EventStore
from src.firm.memedesk import logic
from src.firm.memedesk.desk import MemeDesk

NOW = 1_800_000_000.0
SAFE = {"mint_authority": None, "freeze_authority": None, "lp_locked_pct": 100, "top_holder_pct": 3.0,
        "top10_pct": 18.0, "graph_insiders": 0, "insiders_flagged": 0, "score": 1, "risks": [], "rugged": False,
        "total_holders": 3000, "creator_balance": 0}


def pair(mint, **kw):
    base = {"mint": mint, "pair": "P" + mint, "dex": "pumpswap", "symbol": mint, "name": mint, "price_usd": 0.001,
            "liquidity_usd": 90_000, "volume": {"m5": 15_000, "h1": 60_000, "h6": 0, "h24": 0}, "buys_m5": 400,
            "sells_m5": 200, "buys_h1": 0, "sells_h1": 0, "change": {"m5": 6.0, "h1": 0, "h6": 0, "h24": 0},
            "created_ms": (NOW - 3 * 3600) * 1000, "market_cap": 1e6, "socials": ["twitter", "telegram"],
            "websites": 1, "boosts": 500}
    return {**base, **kw}


def breakout():
    cs = [{"ts": i, "o": 1.0, "h": 1.08, "l": 0.96, "c": 1.0, "v": 1} for i in range(20)]
    return cs + [{"ts": 20, "o": 1.0, "h": 1.14, "l": 0.99, "c": 1.12, "v": 1}]


class FakeSources:
    def __init__(self):
        self.pairs = {"GOOD": pair("GOOD"), "RUG": pair("RUG"), "PUMP": pair("PUMP", change={"m5": 45.0, "h1": 0,
                                                                                         "h6": 0, "h24": 0})}
        self.value_mult = 1.0

    async def trending_tokens(self):
        return [{"mint": m, "boost": 500, "links": ["twitter"], "description": ""} for m in self.pairs]

    async def pair(self, mint):
        return self.pairs[mint]

    async def safety_report(self, mint):
        return {**SAFE, "mint_authority": "EVIL" if mint == "RUG" else None}

    async def roundtrip_loss_pct(self, mint, sol=0.1):
        return 3.0, {}

    async def candles(self, pool, minutes=5, limit=48):
        return breakout()

    async def quote(self, i, o, amount, slippage_bps=300):
        if i.startswith("So111"):                       # buy: 1 SOL -> 1e6 token units
            return {"in": amount, "out": int(amount / 1e9 * 1_000_000), "impact_pct": 1.0, "route": ["Pump.fun Amm"]}
        return {"in": amount, "out": int(amount / 1_000_000 * 1e9 * self.value_mult), "impact_pct": 1.0, "route": []}


def desk(tmp_path, src, now):
    store = EventStore(str(tmp_path / "m.db"))
    d = MemeDesk(src, store, cfg=logic.load_config(), limits=logic.load_limits(), data_dir=tmp_path / "md",
                 journal_dir=tmp_path / "j", wallets=[], now_fn=lambda: now[0])
    return d, store


def types(store, t):
    return [json.loads(e["payload_json"]) for e in store.get_events(types=[t])]


def test_paper_round_trip_with_veto_no_chase_free_ride_and_kill_switch(tmp_path):
    now, src = [NOW], FakeSources()
    d, store = desk(tmp_path, src, now)
    decisions = {r["mint"]: r["decision"] for r in asyncio.run(d.scan())}
    assert decisions == {"GOOD": "buy", "RUG": "veto_safety", "PUMP": "skip"}
    assert any("Mint-Authority" in r for v in types(store, "MEME_VETO") for r in v["reasons"])
    assert "senkrechter Pump" in types(store, "MEME_SKIP")[0]["reason"]
    pos = d.state["positions"]["GOOD"]
    assert 0.14 < pos["cost_sol"] < 0.16                       # 1.5 % of 10 SOL + priority fee
    src.value_mult = 2.3                                       # token more than doubled
    now[0] += 60
    asyncio.run(d.manage())
    part = types(store, "MEME_TRADE_PARTIAL")
    assert part and part[0]["reason"].startswith("Free Ride") and d.state["positions"]["GOOD"]["free_ride"]
    (tmp_path / "md" / "halt").write_text("operator")
    now[0] += 60
    asyncio.run(d.manage())
    closed = types(store, "MEME_TRADE_CLOSE")
    assert closed and "Kill-Switch" in closed[0]["reason"] and closed[0]["pnl_sol"] > 0
    assert d.state["positions"] == {} and d.state["halted"] == "Kill-Switch"
    lines = (tmp_path / "j" / "journal.jsonl").read_text().splitlines()
    assert [json.loads(l)["type"] for l in lines] == ["OPEN", "PARTIAL", "CLOSE"]
    assert json.loads(lines[0])["snapshot"]["safety"]["lp_locked_pct"] == 100   # data "screenshot" at entry
    assert asyncio.run(d.scan()) == []                         # halted: no new trades


def test_hard_stop_and_daily_loss_pause(tmp_path):
    now, src = [NOW], FakeSources()
    d, store = desk(tmp_path, src, now)
    asyncio.run(d.scan())
    src.value_mult = 0.6
    now[0] += 60
    asyncio.run(d.manage())
    assert types(store, "MEME_TRADE_CLOSE")[0]["reason"].startswith("harter Stop")
    d.state["equity_mark"] = d.state["equity"] = 9.0        # simulate a -10 % day
    d.state["positions"] = {}
    asyncio.run(d.manage())
    assert any(l["limit"] == "daily_loss" for l in types(store, "MEME_LIMIT"))
    assert logic.risk_check(d.state, d.limits, d.state["day"])[0].startswith("Tagesverlust")


def test_state_survives_restart(tmp_path):
    now, src = [NOW], FakeSources()
    d, store = desk(tmp_path, src, now)
    asyncio.run(d.scan())
    d2 = MemeDesk(src, store, cfg=logic.load_config(), limits=logic.load_limits(), data_dir=tmp_path / "md",
                  journal_dir=tmp_path / "j", wallets=[], now_fn=lambda: now[0])
    assert "GOOD" in d2.state["positions"] and d2.state["equity"] < 10
