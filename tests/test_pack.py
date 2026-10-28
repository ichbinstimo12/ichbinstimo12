import json
import tempfile
import time
import unittest
from pathlib import Path

from src.firm.money import journal
from src.firm.money.signal_v2 import SignalEngineV2
from src.firm.money.sizing import size_position_v2
from src.firm.money.strategy_gate import StrategyGate
from src.firm.research import history, lab
from tests.synth import make


def desk_candles(cs):
    from datetime import datetime, timezone
    return [{"t": datetime.fromtimestamp(c["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "open": c["o"], "high": c["h"],
             "low": c["l"], "close": c["c"], "volume": 1} for c in cs]


class SizingTests(unittest.TestCase):
    M = {"size_step": 0.0001, "min_size": 0.0001, "max_size": 1000.0}

    def test_fx_makes_real_risk_match_budget(self):
        # BTCUSD in USD, Konto CHF (USDCHF=0.80): Budget 5 CHF, Stop 500 USD
        size, why, risk = size_position_v2(80000, 80500, 1000, 0.005, 0.80, self.M)
        self.assertAlmostEqual(risk, 5.0, delta=0.05)
        old = 5.0 / 500  # v1: Budget / Stop ohne FX
        self.assertGreater(size, old)  # v1 war ~20 % zu klein -> echtes Risiko nur ~0.8 R

    def test_min_size_blocks_trade(self):
        s, why, r = size_position_v2(100, 99, 200, 0.005, 1.0, {"size_step": 1, "min_size": 5, "max_size": 100})
        self.assertEqual(s, 0.0); self.assertIn("Mindestgroesse", why)

    def test_risk_factor_halves(self):
        a = size_position_v2(100, 99, 10000, 0.005, 1.0, {"size_step": 0.01, "min_size": 0.01})[0]
        b = size_position_v2(100, 99, 10000, 0.005, 1.0, {"size_step": 0.01, "min_size": 0.01}, risk_factor=0.5)[0]
        self.assertAlmostEqual(b, a / 2, places=1)


class SignalV2Tests(unittest.TestCase):
    def test_closed_candles_only_and_once_per_bar_and_edge(self):
        cs = make(1500, seed=21, regime=True, mu=0.0015, vol=0.002)
        raw = desk_candles(cs)
        fired = 0
        for end in range(400, 1500, 1):
            now = cs[end]["ts"] + 10          # Kerze `end` ist gerade geschlossen+10s (naechste laeuft)
            eng = SignalEngineV2("donchian_trend", resolution="HOUR", now_fn=lambda n=now: n)
            s1 = eng.evaluate("X", raw[:end + 2], 100, 100.01)
            s2 = eng.evaluate("X", raw[:end + 2], 100, 100.01)
            self.assertIsNone(s2, "dieselbe Kerze darf nicht zweimal ausgewertet werden")
            fired += 1 if s1 else 0
        self.assertGreater(fired, 0)

    def test_forming_candle_ignored(self):
        cs = make(600, seed=3)
        raw = desk_candles(cs)
        now = cs[-1]["ts"] + 100  # letzte Kerze laeuft noch
        eng = SignalEngineV2("donchian_trend", now_fn=lambda: now)
        eng.evaluate("X", raw)
        self.assertEqual(eng._last_bar["X"], cs[-2]["ts"])

    def test_wide_spread_blocks(self):
        cs = make(1500, seed=21, regime=True, mu=0.0015, vol=0.002)
        raw = desk_candles(cs)
        for end in range(400, 1500):
            now = cs[end]["ts"] + 10
            a = SignalEngineV2("donchian_trend", now_fn=lambda n=now: n)
            if a.evaluate("X", raw[:end + 2], 100, 100.001):
                b = SignalEngineV2("donchian_trend", now_fn=lambda n=now: n)
                self.assertIsNone(b.evaluate("X", raw[:end + 2], 100, 120))  # Spread riesig
                return
        self.fail("kein Signal gefunden")


class JournalTests(unittest.TestCase):
    def ev(self, t, typ, p, agent="X"):
        return {"ts": f"2026-10-24T{t}", "agent": agent, "type": typ, "p": p}

    def test_pnl_from_balances_and_stats(self):
        e = [self.ev("10:00:00", "ACCOUNT_SNAPSHOT", {"balance": 1000.0, "equity": 1000.0}),
             self.ev("10:01:00", "SIGNAL", {"epic": "B", "price": 100, "stop_loss": 99, "side": "BUY"}),
             self.ev("10:01:01", "ORDER_FILLED", {"epic": "B", "side": "BUY", "size": 4.0, "price": 100}),
             self.ev("10:05:00", "ACCOUNT_SNAPSHOT", {"balance": 996.0, "equity": 996.0}),
             self.ev("10:06:00", "POSITION_CLOSED", {"epic": "B", "direction": "BUY"}),
             self.ev("10:07:00", "ACCOUNT_SNAPSHOT", {"balance": 996.0, "equity": 996.0})]
        t = journal.reconstruct_trades(e)
        self.assertEqual(len(t), 1); self.assertAlmostEqual(t[0]["pnl"], -4.0); self.assertAlmostEqual(t[0]["r"], -1.0)
        st = journal.stats(t)
        self.assertFalse(st["verdict_possible"]); self.assertEqual(st["closed"], 1)

    def test_health_kill_criteria(self):
        h = journal.StrategyHealth(expected_expectancy_r=0.2)
        self.assertEqual(h.check([-1] * 6)["status"], "STOP")
        self.assertEqual(h.check([1, -1, 1, -1])["status"], "OK")
        self.assertEqual(h.check([-1, -1, 0.5] * 10)["status"], "STOP")  # E = -0.5 R nach 30 Trades
        self.assertEqual(h.check([0.1, -0.2] * 15)["status"], "WARN")

    def test_desk_rules(self):
        r = journal.DeskRules(max_trades_per_day=2, max_consecutive_losses=2, cooldown_bars_after_stop=6)
        t = 1_700_000_000.0
        self.assertTrue(r.can_enter("B", "SELL", t)[0])
        r.on_close("B", "SELL", -4.0, t)
        self.assertFalse(r.can_enter("B", "SELL", t + 3600)[0], "kein Re-Entry gleiche Richtung direkt nach Stop")
        self.assertTrue(r.can_enter("B", "BUY", t + 3600)[0])
        r.on_close("E", "BUY", -4.0, t + 100)
        self.assertIn("Pause", r.can_enter("Z", "BUY", t + 200)[1])
        r2 = journal.DeskRules(max_trades_per_day=1)
        r2.on_entry(t)
        self.assertIn("Tageslimit", r2.can_enter("Z", "BUY", t)[1])


class GateAndHistoryTests(unittest.TestCase):
    def test_gate(self):
        d = Path(tempfile.mkdtemp()) / "v.json"
        g = StrategyGate(d)
        self.assertFalse(g.decision("BTCUSD", True)[0])
        d.write_text(json.dumps({"created": time.time(), "verdicts": {
            "A": {"verdict": "PASS", "strategy": "donchian_trend", "params": {}},
            "B": {"verdict": "FORWARD_TEST", "strategy": "ts_momentum", "params": {}},
            "C": {"verdict": "FAIL", "reasons": ["x"]}}}))
        self.assertEqual(g.decision("A", False)[3], 1.0)
        self.assertTrue(g.decision("B", True)[0]); self.assertEqual(g.decision("B", True)[3], 0.5)
        self.assertFalse(g.decision("B", False)[0], "FORWARD_TEST nie live")
        self.assertFalse(g.decision("C", True)[0])
        d.write_text(json.dumps({"created": time.time() - 40 * 86400, "verdicts": {"A": {"verdict": "PASS", "strategy": "x"}}}))
        self.assertFalse(g.decision("A", True)[0])

    def test_history_paging_and_quality(self):
        from datetime import datetime, timezone
        cs = make(5000, seed=9, t0=int(time.time()) - 5000 * 3600)
        def fake(epic, res, a, b):
            ta = datetime.fromisoformat(a).replace(tzinfo=timezone.utc).timestamp()
            tb = datetime.fromisoformat(b).replace(tzinfo=timezone.utc).timestamp()
            rows = [c for c in cs if ta <= c["ts"] <= tb][:1000]
            return [{"snapshotTimeUTC": datetime.fromtimestamp(c["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                     "openPrice": {"bid": c["o"], "ask": c["o"]}, "highPrice": {"bid": c["h"], "ask": c["h"]},
                     "lowPrice": {"bid": c["l"], "ask": c["l"]}, "closePrice": {"bid": c["c"] - 0.01, "ask": c["c"] + 0.01}} for c in rows]
        out = history.fetch_range(fake, "X", "HOUR", days=200, sleep=0)
        self.assertGreater(len(out), 4000)
        self.assertEqual(len({c["ts"] for c in out}), len(out), "keine Duplikate")
        self.assertTrue(all(out[i]["ts"] < out[i + 1]["ts"] for i in range(len(out) - 1)))
        q = history.quality_report(out, "HOUR")
        self.assertEqual(q["big_gaps"], 0)
        p = Path(tempfile.mkdtemp()) / "x.csv"
        history.save_csv(p, out)
        self.assertEqual(len(history.load_csv(p)), len(out))


if __name__ == "__main__":
    unittest.main()
