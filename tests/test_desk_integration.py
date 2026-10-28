"""Rauchtest des gepatchten Desks mit Attrappen (Broker/Gateway/RiskGate), ohne httpx/pydantic."""
import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.synth import make

ROOT = Path(__file__).resolve().parent.parent


def load_desk_module():
    stubs = {}
    es = types.ModuleType("src.firm.bus.event_store")
    class EventStore:
        def __init__(self): self.events = []
        def append(self, agent, typ, payload, *a, **k): self.events.append((agent, typ, payload))
    es.EventStore = EventStore
    gw = types.ModuleType("src.firm.money.execution_gateway")
    class TradeProposal:
        def __init__(self, **kw): self.__dict__.update(kw)
    class ExecutionGateway:
        def __init__(self): self.sent = []
        def _live_approved(self): return False
        async def execute(self, proposal, **kw):
            self.sent.append(proposal)
            return {"status": "FILLED", "order_id": "O1", "filled_price": proposal.stop_loss, "filled_size": proposal.size}
    gw.TradeProposal, gw.ExecutionGateway = TradeProposal, ExecutionGateway
    rg = types.ModuleType("src.firm.money.risk_gate")
    class RiskGate:
        def check_account_health(self, *a): return True, ""
    rg.RiskGate = RiskGate
    for m in (es, gw, rg):
        sys.modules[m.__name__] = m
    for pkg in ("src.firm.bus",):
        sys.modules.setdefault(pkg, types.ModuleType(pkg))
    spec = importlib.util.spec_from_file_location("desk_under_test", ROOT / "src/firm/money/trading_desk.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, EventStore, ExecutionGateway, RiskGate


def to_desk(cs):
    return [{"t": datetime.fromtimestamp(c["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "open": c["o"], "high": c["h"],
             "low": c["l"], "close": c["c"], "volume": 1} for c in cs]


class FakeBroker:
    is_demo = True
    def __init__(self, candles, quote="USD"):
        self.candles, self.quote, self.positions = candles, quote, []
    async def get_account_details(self): return {"balance": 1000.0, "equity": 1000.0, "currency": "CHF"}
    async def get_positions(self): return list(self.positions)
    async def get_candles(self, epic, res, limit): return self.candles
    async def get_market_details(self, epic):
        if epic == "USDCHF":
            return {"bid": 0.79, "ask": 0.81, "status": "TRADEABLE"}
        if epic != "BTCUSD":
            raise KeyError(f"unbekannter Markt {epic}")
        return {"epic": epic, "status": "TRADEABLE", "bid": self.candles[-1]["close"] - 0.01, "ask": self.candles[-1]["close"] + 0.01,
                "min_size": 0.0001, "max_size": 1e6, "size_step": 0.0001, "currency": self.quote}
    async def place_order(self): pass


class DeskIntegration(unittest.TestCase):
    def setUp(self):
        self.mod, ES, GW, RG = load_desk_module()
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "wl.toml").write_text('allowed_epics = ["BTCUSD"]\n')
        (self.tmp / "lim.toml").write_text("[account]\nmax_leverage=10.0\nmax_positions=3\ndaily_loss_limit=0.02\nmax_drawdown_limit=0.08\n[trade]\nmax_risk_per_trade=0.005\n")
        self.cs = make(1500, seed=21, regime=True, mu=0.0015, vol=0.002, t0=int(time.time()) - 1500 * 3600 - 30)
        self.ES, self.GW, self.RG = ES, GW, RG

    def desk(self, verdicts, broker=None, end=None):
        from src.firm.money.strategy_gate import StrategyGate
        vp = self.tmp / "v.json"
        vp.write_text(json.dumps({"created": time.time(), "verdicts": verdicts}))
        b = broker or FakeBroker(to_desk(self.cs[: end or len(self.cs)]))
        d = self.mod.TradingDesk(b, self.GW(), self.RG(), self.ES(), whitelist_path=str(self.tmp / "wl.toml"),
                                 limits_path=str(self.tmp / "lim.toml"), state_path=str(self.tmp / "st.json"),
                                 gate=StrategyGate(vp))
        return d, b

    def find_signal_end(self):
        from src.firm.money.signal_v2 import SignalEngineV2
        for end in range(500, 1490):
            now = self.cs[end]["ts"] + 10
            e = SignalEngineV2("donchian_trend", now_fn=lambda n=now: n)
            if e.evaluate("BTCUSD", to_desk(self.cs[:end + 2]), 100, 100.01):
                return end, now
        self.fail("kein Signal")

    def run_cycle(self, d, now):
        import src.firm.money.signal_v2 as sv
        orig = time.time
        time.time = lambda: now
        try:
            for e in d._engines.values():
                e.now = lambda: now
            return asyncio.run(d.run_cycle())
        finally:
            time.time = orig

    def test_no_research_no_trade(self):
        d, b = self.desk({})
        s = asyncio.run(d.run_cycle())
        self.assertEqual(s["trades"], [])
        self.assertTrue(any("Forschungsfreigabe" in x["reason"] for x in s["skipped"]))

    def test_fail_verdict_blocks(self):
        d, _ = self.desk({"BTCUSD": {"verdict": "FAIL", "reasons": ["x"]}})
        self.assertEqual(asyncio.run(d.run_cycle())["trades"], [])

    def test_pass_trades_with_fx_correct_size_and_remembers_strategy(self):
        end, now = self.find_signal_end()
        d, b = self.desk({"BTCUSD": {"verdict": "PASS", "strategy": "donchian_trend", "params": {}}}, end=end + 2)
        d._engine_for("donchian_trend", {}).now = lambda: now
        import time as _t
        orig = _t.time; _t.time = lambda: now
        try:
            d._engine_for("donchian_trend", {}).now = lambda: now
            s = asyncio.run(d.run_cycle())
        finally:
            _t.time = orig
        self.assertEqual(len(s["trades"]), 1, s)
        p = d.gateway.sent[0]
        entry = [e for e in d.event_store.events if e[1] == "SIGNAL"][0][2]["price"]
        dist = abs(p.stop_loss - entry)
        real_risk_chf = p.size * dist * 0.80   # USD -> CHF
        self.assertAlmostEqual(real_risk_chf, 5.0, delta=0.15)  # 0.5 % von 1000 CHF, FX-korrekt
        self.assertEqual(d.state.data["pos_meta"]["BTCUSD"]["strategy"], "donchian_trend")

    def test_unknown_quote_currency_does_not_trade(self):
        end, now = self.find_signal_end()
        b = FakeBroker(to_desk(self.cs[: end + 2]), quote="XYZ")
        d, _ = self.desk({"BTCUSD": {"verdict": "PASS", "strategy": "donchian_trend", "params": {}}}, broker=b)
        d._engine_for("donchian_trend", {}).now = lambda: now
        s = asyncio.run(d.run_cycle())
        self.assertEqual(s["trades"], [])

    def test_broker_closed_position_feeds_loss_rules(self):
        d, b = self.desk({})
        d.state.data["pos_meta"] = {"BTCUSD": {"side": "SELL", "strategy": "x", "balance_at_entry": 1000.0}}
        d._detect_closed([], 995.84)
        self.assertEqual(d.rules.state["consec_losses"], 1)
        self.assertFalse(d.rules.can_enter("BTCUSD", "SELL")[0], "kein sofortiger Re-Entry nach Stop")


if __name__ == "__main__":
    unittest.main()
