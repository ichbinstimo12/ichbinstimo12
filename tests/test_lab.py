import copy
import unittest

from src.firm.research import lab
from tests.synth import make


class LabTests(unittest.TestCase):
    def test_no_lookahead_entries_unchanged_by_future(self):
        cs = make(1200, seed=3, regime=True)
        for nm in lab.REGISTRY:
            s = lab.REGISTRY[nm]()
            a = lab.Ind(cs)
            ref = [s.entry(a, i) for i in range(len(cs))]
            mut = copy.deepcopy(cs)
            for c in mut[800:]:
                c["c"] *= 1.5; c["h"] *= 1.6; c["l"] *= 0.5; c["o"] *= 1.2
            b = lab.Ind(mut)
            for i in range(700):
                self.assertEqual(ref[i], s.entry(b, i), f"{nm}: Look-ahead bei Index {i}")

    def test_backtest_prefix_stability(self):
        cs = make(1500, seed=5, regime=True)
        for nm in ("donchian_trend", "ema_state"):
            s = lab.REGISTRY[nm]()
            full = lab.backtest(cs, s)
            part = lab.backtest(cs[:900], s)
            done = [t for t in part if t["exit_i"] < 880]
            self.assertEqual([round(t["r"], 9) for t in done], [round(t["r"], 9) for t in full[:len(done)]])

    def test_costs_monotonic(self):
        cs = make(2500, seed=7, regime=True)
        s = lab.DonchianTrend()
        e = [lab.metrics(lab.backtest(cs, s, cost_mult=m))["expectancy_r"] for m in (0.0, 1.0, 2.0, 4.0)]
        self.assertTrue(all(e[i] >= e[i + 1] for i in range(3)), e)

    def test_stop_first_when_both_hit_and_gap(self):
        base = [{"ts": i * 3600, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0, "spread": 0.0, "v": 1} for i in range(300)]

        class Once(lab.Strategy):
            name = "once"
            def warmup(self): return 100
            def entry(self, ind, i): return ("BUY", 2.0, 2.0) if i == 150 else None

        cs = copy.deepcopy(base)
        cs[152].update(h=103.0, l=97.0)             # Stop UND Ziel in einer Kerze -> Stop zuerst
        t = lab.backtest(cs, Once(), cost_mult=0.0, slip_atr=0.0)
        self.assertEqual((t[0]["why"], round(t[0]["r"], 6)), ("stop", -1.0))
        cs = copy.deepcopy(base)
        cs[152].update(o=90.0, h=91.0, l=89.0, c=90.0)  # Luecke unter dem Stop -> Verlust > 1R
        t = lab.backtest(cs, Once(), cost_mult=0.0, slip_atr=0.0)
        self.assertLess(t[0]["r"], -1.0)

    def test_noise_never_passes_and_edge_is_detectable(self):
        passes = 0
        for seed in range(100, 106):
            rep, _ = lab.research("NOISE", make(3000, seed=seed))
            passes += sum(1 for r in rep if r["verdict"] == "PASS")
        self.assertEqual(passes, 0, "Zufallskurse duerfen nie als PASS gelten")
        found = 0
        for seed in range(200, 204):
            rep, _ = lab.research("TREND", make(4500, seed=seed, regime=True, mu=0.0012, vol=0.002))
            found += sum(1 for r in rep if r["strategy"] in ("donchian_trend", "ts_momentum") and r["verdict"] in ("PASS", "FORWARD_TEST"))
        self.assertGreaterEqual(found, 2, "Das Labor muss einen echten Trend-Edge erkennen koennen")

    def test_trial_counter_grows_and_raises_bar(self):
        _, t1 = lab.research("A", make(2000, seed=1), names=["donchian_trend"])
        _, t2 = lab.research("B", make(2000, seed=2), names=["donchian_trend"], trial_counter_in=t1)
        self.assertGreater(t2, t1)
        self.assertGreater(lab.required_t(500), lab.required_t(5))

    def test_insufficient_data(self):
        rep, _ = lab.research("X", make(300))
        self.assertTrue(all(r["verdict"] == "INSUFFICIENT_DATA" for r in rep))

    def test_normalize_desk_format(self):
        c = [{"t": "2026-10-24T14:00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3}]
        n = lab.normalize(c)[0]
        self.assertEqual((n["o"], n["c"]), (1.0, 1.5)); self.assertGreater(n["ts"], 1e9)


if __name__ == "__main__":
    unittest.main()
