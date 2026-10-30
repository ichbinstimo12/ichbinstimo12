"""Memecoin-Desk: Vetos, 3-von-5-Regel, kein Hinterherrennen, Risiko-Limits."""
import pytest

from src.firm.memedesk import logic

CFG = logic.load_config()
LIM = logic.load_limits()
NOW_MS = 1_800_000_000_000

SAFE = {"mint_authority": None, "freeze_authority": None, "lp_locked_pct": 100, "top_holder_pct": 3.0,
        "top10_pct": 18.0, "graph_insiders": 0, "insiders_flagged": 0, "score": 1, "risks": [], "rugged": False}
PAIR = {"liquidity_usd": 90_000, "volume": {"m5": 15_000, "h1": 60_000}, "buys_m5": 400, "sells_m5": 200,
        "change": {"m5": 6.0}, "created_ms": NOW_MS - 3 * 3.6e6, "socials": ["twitter", "telegram"], "websites": 1,
        "boosts": 500, "symbol": "DUBBLE"}


def consolidation_breakout():
    cs = [{"ts": i, "o": 1.0, "h": 1.08, "l": 0.96, "c": 1.0} for i in range(20)]
    cs.append({"ts": 20, "o": 1.0, "h": 1.14, "l": 0.99, "c": 1.12})
    return cs


def test_safety_vetoes_each_danger_alone():
    assert logic.safety_veto(SAFE, 4.0, PAIR, CFG) == []
    for change, word in [({"mint_authority": "X"}, "Mint"), ({"freeze_authority": "X"}, "Freeze"),
                         ({"lp_locked_pct": 40}, "LP"), ({"top_holder_pct": 22}, "Einzel-Holder"),
                         ({"graph_insiders": 2}, "Bundle"), ({"risks": [("Copycat token", "danger")]}, "Copycat")]:
        reasons = logic.safety_veto({**SAFE, **change}, 4.0, PAIR, CFG)
        assert any(word in r for r in reasons), (change, reasons)
    assert any("Honeypot" in r for r in logic.safety_veto(SAFE, 40.0, PAIR, CFG))
    assert logic.safety_veto(SAFE, None, PAIR, CFG)[0].startswith(logic.DATA_MISSING)
    assert logic.safety_veto(None, 4.0, PAIR, CFG)[0].startswith(logic.DATA_MISSING)


def test_three_of_five_enters_and_two_does_not():
    sig = logic.evaluate_signals(PAIR, [], SAFE, narrative=0.7, holders_growth=0.05, smart_buyers=0,
                                 candles=consolidation_breakout(), cfg=CFG)
    assert sig["signals"]["smart_money"] is False and sig["count"] >= 3 and sig["enter"] is True
    weak = logic.evaluate_signals({**PAIR, "volume": {"m5": 1000, "h1": 60_000}}, [], SAFE, narrative=0.2,
                                  holders_growth=-0.1, smart_buyers=0, candles=consolidation_breakout(), cfg=CFG)
    assert weak["count"] == 2 and weak["enter"] is False


def test_never_chase_a_vertical_pump():
    cs = [{"ts": i, "o": 1.0 + i * 0.1, "h": 1.1 + i * 0.1, "l": 0.95 + i * 0.1, "c": 1.05 + i * 0.1} for i in range(20)]
    cs.append({"ts": 20, "o": 3.0, "h": 4.4, "l": 3.0, "c": 4.2})
    ok, vertical, why = logic.structure_ok(cs, CFG["thresholds"]["chase_m5_pct"])
    assert vertical and not ok and "senkrecht" in why
    sig = logic.evaluate_signals({**PAIR, "change": {"m5": 40.0}}, [], SAFE, 0.9, 0.2, 3, cs, CFG)
    assert sig["count"] >= 3 and sig["chasing"] and sig["enter"] is False


def test_safety_veto_blocks_even_with_five_signals():
    sig = logic.evaluate_signals(PAIR, ["Mint-Authority aktiv"], SAFE, 0.9, 0.2, 3, consolidation_breakout(), CFG)
    assert sig["enter"] is False


def test_narrative_penalises_copycat_waves():
    fresh, _ = logic.narrative_score({"boost": 500}, PAIR, ["DUBBLE"], now_ms=NOW_MS)
    stale, why = logic.narrative_score({"boost": 500}, PAIR, ["DUBBLE", "DUBBLE2", "dubbleinu", "BabyDubble"],
                                       now_ms=NOW_MS)
    assert fresh > stale and any("durchgekaut" in w for w in why)


def test_risk_manager_sizes_and_vetoes():
    state = {"equity": 10.0, "positions": {}}
    veto, size = logic.risk_check(state, LIM, "2026-10-29")
    assert veto is None and size == pytest.approx(10.0 * LIM["trade"]["risk_per_trade"])
    full = {"equity": 10.0, "positions": {str(i): {} for i in range(LIM["trade"]["max_open_positions"])}}
    assert "offene Positionen" in logic.risk_check(full, LIM, "2026-10-29")[0]


def test_daily_loss_pauses_until_tomorrow_and_drawdown_halts():
    st = {"equity": 10.0}
    assert logic.update_limits(st, LIM, "d1") is None
    st["equity_mark"] = 9.4                                    # -6 % today
    assert logic.update_limits(st, LIM, "d1") == "pause"
    assert logic.risk_check({**st, "positions": {}}, LIM, "d1")[0].startswith("Tagesverlust")
    assert logic.risk_check({**st, "positions": {}}, LIM, "d2")[0] is None   # next day trading again
    st["equity_mark"] = 7.9                                    # -21 % from peak 10
    assert logic.update_limits(st, LIM, "d2") == "halt" and "Drawdown" in st["halted"]


def test_limits_reject_more_than_two_percent(tmp_path):
    p = tmp_path / "l.toml"
    p.write_text((open("config/memedesk_limits.toml").read()).replace("risk_per_trade = 0.015", "risk_per_trade = 0.05"))
    with pytest.raises(ValueError):
        logic.load_limits(p)


def test_coach_override_is_clamped_to_bounds(tmp_path):
    import json
    o = tmp_path / "params.json"
    o.write_text(json.dumps({"weights": {"narrative": 1.7, "structure": 9.0}, "thresholds": {"narrative_min": 0.4}}))
    cfg = logic.load_config(override=o)
    assert cfg["weights"]["narrative"] == 1.7 and cfg["weights"]["structure"] == 1.0   # 9.0 outside bounds: ignored
    assert cfg["thresholds"]["narrative_min"] == 0.4


def test_paid_boost_and_socials_alone_are_not_a_narrative():
    """Echte Laeufe: Narrativ war bei 100 % der Tokens 'wahr' - bezahlte Boosts + Socials sind keine Aufmerksamkeit."""
    paid_only = {**PAIR, "buys_h1": 150, "created_ms": NOW_MS - 30 * 3.6e6}
    score, _ = logic.narrative_score({"boost": 500, "sources": ["boost"]}, paid_only, ["DUBBLE"], now_ms=NOW_MS)
    assert score < CFG["thresholds"]["narrative_min"]
    organic = {**PAIR, "buys_h1": 5000}
    score2, why = logic.narrative_score({"boost": 0, "sources": ["gecko_trending"]}, organic, ["DUBBLE"], now_ms=NOW_MS)
    assert score2 >= CFG["thresholds"]["narrative_min"] and any("Trending" in w for w in why)


def test_no_entry_without_money_flow():
    """Alle 4 echten Kaeufe hatten nur Narrativ+Verteilung+Struktur, kein Volumen/Smart Money -> kein Einstieg mehr."""
    no_flow = {**PAIR, "volume": {"m5": 1000, "h1": 60_000}}
    sig = logic.evaluate_signals(no_flow, [], SAFE, narrative=0.9, holders_growth=None, smart_buyers=None,
                                 candles=consolidation_breakout(), cfg=CFG)
    assert sig["count"] >= 3 and sig["enter"] is False and sig["no_flow"] is True
    flow = logic.evaluate_signals(PAIR, [], SAFE, narrative=0.9, holders_growth=0.05, smart_buyers=None,
                                  candles=consolidation_breakout(), cfg=CFG)
    assert flow["enter"] is True and flow["metrics"]["smart_buyers"] is None
