"""Lernendes Modell, Uebernacht-Kosten, Forschungspipeline (EXPLORE-Spur, fortgeschriebener Trial-Counter)."""
import copy
import json

from src.firm.money.signal_v2 import SignalEngineV2
from src.firm.money.strategy_gate import StrategyGate
from src.firm.research import lab, pipeline
from tests.synth import make


def _predictable(n=3000, seed=11):
    """Kurse mit echter, lernbarer Struktur: kurzfristige Rendite setzt sich fort (Momentum im Rauschen)."""
    import random
    rnd, px, prev, out = random.Random(seed), 100.0, 0.0, []
    for i in range(n):
        r = 0.55 * prev + rnd.gauss(0, 0.003)
        o, c = px, px * (1 + r)
        out.append({"ts": 1_700_000_000 + i * 3600, "o": o, "h": max(o, c) * 1.0008, "l": min(o, c) * 0.9992,
                    "c": c, "spread": c * 0.0001, "v": 1})
        px, prev = c, r
    return out


def test_ml_training_never_sees_the_future():
    cs = make(1200, seed=3, regime=True)
    s1 = lab.MLLogit(horizon=12, k=1.5, thr=0.55)
    a = lab.Ind(cs)
    s1.prepare(a, 600)
    ref = [s1.entry(a, i) for i in range(700)]
    mut = copy.deepcopy(cs)
    for c in mut[800:]:
        c["c"] *= 1.5; c["h"] *= 1.6; c["l"] *= 0.5; c["o"] *= 1.2
    s2 = lab.MLLogit(horizon=12, k=1.5, thr=0.55)
    b = lab.Ind(mut)
    s2.prepare(b, 600)
    assert s1.weights == s2.weights
    assert ref == [s2.entry(b, i) for i in range(700)]


def test_ml_learns_real_structure_and_not_noise():
    good = lab.backtest(_predictable(), lab.MLLogit(horizon=12, k=1.5, thr=0.56), cost_mult=0.5)
    assert lab.metrics(good)["expectancy_r"] > 0.05, lab.metrics(good)
    noise_rep, _ = lab.research("NOISE", make(3000, seed=101), names=["ml_logit"])
    assert noise_rep[0]["verdict"] != "PASS"


def test_overnight_financing_costs_money():
    cs = make(2500, seed=7, regime=True)
    s = lab.DonchianTrend()
    free = lab.metrics(lab.backtest(cs, s))["expectancy_r"]
    paid = lab.metrics(lab.backtest(cs, s, overnight_pct_day=0.001, bar_seconds=3600))["expectancy_r"]
    assert paid < free


def test_exported_ml_weights_reproduce_lab_signals_live():
    cs = _predictable(1500)
    strat = lab.MLLogit(horizon=12, k=1.5, thr=0.56)
    ind = lab.Ind(cs)
    strat.prepare(ind, ind.n)
    params = strat.export()
    json.dumps(params)  # must be storable in strategy_verdicts.json
    clone = lab.MLLogit(**params)
    assert [clone.entry(ind, i) for i in range(1300, 1500)] == [strat.entry(ind, i) for i in range(1300, 1500)]
    engine = SignalEngineV2("ml_logit", params, resolution="HOUR", now_fn=lambda: cs[-1]["ts"] + 3600)
    assert engine.strat.weights == strat.weights


def test_pipeline_writes_verdicts_and_continues_the_trial_counter(tmp_path):
    data = {"NOISE": make(2000, seed=5), "TREND": make(4500, seed=201, regime=True, mu=0.0012, vol=0.002)}
    verdicts, lines, trials = pipeline.run_research(data, "HOUR", trials_in=1000, log=lambda *_: None)
    assert trials > 1000
    path = pipeline.write_outputs(verdicts, lines, trials, "HOUR", tmp_path)
    saved = json.loads(path.read_text())
    assert saved["resolution"] == "HOUR" and saved["trials"] == trials
    assert pipeline.previous_trials(path) == trials
    assert (tmp_path / "research_report.md").exists()
    assert set(saved["verdicts"]) == {"NOISE", "TREND"}


def test_gate_live_mode_never_trades_forward_test_or_explore(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRM_DEMO_EXPLORE", "1")
    monkeypatch.setenv("FIRM_STYLE", "swing")
    path = tmp_path / "v.json"
    import time
    path.write_text(json.dumps({"created": time.time(), "resolution": "HOUR", "verdicts": {
        "A": {"verdict": "FORWARD_TEST", "strategy": "donchian_trend", "params": {}},
        "B": {"verdict": "FAIL", "explore": {"strategy": "ts_momentum", "params": {}, "oos_expectancy_r": 0.03}},
        "C": {"verdict": "PASS", "strategy": "rsi_reversion", "params": {}},
    }}))
    gate = StrategyGate(str(path))
    assert gate.decision("A", True, "PAPER")[0] and gate.decision("A", True, "PAPER")[3] == 0.5
    assert gate.decision("B", True, "PAPER")[3] == 0.25
    for epic in ("A", "B"):
        assert not gate.decision(epic, True, "LIVE_MICRO")[0], "LIVE never trades unproven strategies"
        assert not gate.decision(epic, False, "PAPER")[0], "non-demo broker never trades unproven strategies"
    assert gate.decision("C", False, "LIVE")[0]


def test_minimum_size_feasibility_blocks_untradeable_markets():
    rep = {"extra": {"median_stop_pct": 0.03}}
    # BTC-like: price 100k, min 0.01, 3 % stop -> 30 USD at risk vs 5 CHF budget
    why = pipeline.size_feasibility(rep, {"price": 100_000, "min_size": 0.01, "fx": 0.8},
                                    {"balance": 1000, "risk_pct": 0.005}, "PASS")
    assert why and "nicht handelbar" in why
    assert pipeline.size_feasibility(rep, {"price": 1.1, "min_size": 100, "fx": 0.8},
                                     {"balance": 1000, "risk_pct": 0.005}, "PASS") is None
    assert pipeline.size_feasibility(rep, None, None, "PASS") is None


def test_report_contains_verdict_table_and_regimes(tmp_path):
    data = {"TREND": make(4500, seed=201, regime=True, mu=0.0012, vol=0.002)}
    _, lines, _ = pipeline.run_research(data, "HOUR", log=lambda *_: None)
    text = "\n".join(lines)
    assert "| Strategy | Markt | Timeframe | Sharpe" in text
    assert "Regime:" in text and "high_vol" in text


def test_gate_blocks_size_infeasible_verdicts(tmp_path):
    import time
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"created": time.time(), "verdicts": {
        "BTCUSD": {"verdict": "PASS", "strategy": "donchian_trend", "params": {}, "size_feasible": False,
                   "size_reason": "Mindestgroesse zu gross"}}}))
    ok, *_, why = StrategyGate(str(path)).decision("BTCUSD", True, "PAPER")
    assert not ok and "Mindestgroesse" in why
