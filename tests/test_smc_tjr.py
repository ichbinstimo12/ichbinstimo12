"""TJR-Stil (Smart-Money-Konzepte): Sweep der Vor-Sitzungs-Liquiditaet -> Strukturbruch -> Einstieg im Fair Value Gap."""
import copy

from src.firm.research import lab

DAY0 = 1_700_006_400 - (1_700_006_400 % 86400)   # UTC midnight
STEP = 300                                         # 5-Minuten-Kerzen
SESSION = dict(intraday=True, entry_start_h=7.0, entry_end_h=16.0, flat_h=19.0)


def bar(i, o, h, l, c):
    return {"ts": DAY0 + i * STEP, "o": o, "h": h, "l": l, "c": c}


def bullish_day():
    """Asia-Spanne 99..101 (00-07 UTC), um 07:xx Sweep unter 99 mit Schluss darueber, dann Verdraengung nach oben
    mit Luecke (FVG) und Bruch des letzten Hochs, danach Ruecksetzer in die Luecke."""
    cs, i = [], 0
    while i < 84:                                   # 00:00 - 06:55: Range
        p = 100 + (0.8 if i % 2 else -0.8)
        cs.append(bar(i, 100, max(100, p) + 0.2, min(100, p) - 0.2, p)); i += 1
    seq = [  # o, h, l, c
        (100.0, 100.3, 99.6, 99.8),
        (99.8, 99.9, 98.4, 99.3),     # Sweep: Tief unter 98.8 (Asia-Tief), Schluss zurueck darueber
        (99.3, 100.0, 99.2, 99.9),
        (99.9, 101.6, 99.9, 101.5),   # Verdraengung
        (101.5, 102.4, 100.6, 102.2), # Tief 100.6 > Hoch zwei Kerzen zuvor (100.0) -> FVG 100.0..100.6, Bruch > 101.2
        (102.2, 102.3, 101.4, 101.6),
        (101.6, 101.7, 100.4, 100.9), # Ruecksetzer in die Luecke, Schluss ueber dem Lueckenboden -> Einstieg
    ]
    for o, h, l, c in seq:
        cs.append(bar(i, o, h, l, c)); i += 1
    while i < 220:
        cs.append(bar(i, 101, 101.3, 100.7, 101)); i += 1
    return cs


def signals(cs, **kw):
    s = lab.REGISTRY["smc_tjr"](**SESSION, **kw)
    ind = lab.Ind(cs)
    return [(i, s.entry(ind, i)) for i in range(len(cs)) if s.entry(ind, i)]


def test_sweep_structure_break_and_fvg_retrace_gives_a_long_with_stop_below_the_sweep():
    sig = signals(bullish_day())
    assert len(sig) == 1
    i, (side, stop_dist, tp_dist) = sig[0]
    assert i == 90 and side == "BUY"
    close = bullish_day()[i]["c"]
    assert close - stop_dist < 98.4                 # Stop unter dem Sweep-Tief
    assert abs(tp_dist - 2.0 * stop_dist) < 1e-9    # Ziel 2R (Standard)


def test_no_trade_without_a_liquidity_sweep():
    cs = bullish_day()
    cs[85] = bar(85, 99.8, 99.9, 99.0, 99.3)       # Tief bleibt ueber dem Asia-Tief -> kein Sweep
    assert signals(cs) == []


def test_mirror_image_gives_a_short():
    cs = [{**c, "o": 200 - c["o"], "h": 200 - c["l"], "l": 200 - c["h"], "c": 200 - c["c"]} for c in bullish_day()]
    sig = signals(cs)
    assert len(sig) == 1 and sig[0][1][0] == "SELL"


def test_no_lookahead_future_bars_do_not_change_past_signals():
    cs = bullish_day()
    ref = signals(cs)
    mut = copy.deepcopy(cs)
    for c in mut[100:]:
        c["h"] += 5; c["l"] -= 5; c["c"] += 3
    assert [s for s in signals(mut) if s[0] < 95] == [s for s in ref if s[0] < 95]


def test_registered_for_daytrading_research():
    assert "smc_tjr" in lab.DAYTRADE_SET and "smc_tjr" in lab.INTRADAY_ONLY and lab.grid_points("smc_tjr")


def test_context_reports_levels_and_stage():
    s = lab.REGISTRY["smc_tjr"](**SESSION)
    cs = bullish_day()
    ctx = s.context(lab.Ind(cs[:89]))                 # after structure break + gap, before the retrace
    assert ctx["pre_session_low"] == 99.0 and ctx["stage"] == "fvg" and ctx["side"] == "BUY"
    assert ctx["sweep_extreme"] == 98.4
    assert s.context(lab.Ind(cs[:91]))["stage"] == "entry"
