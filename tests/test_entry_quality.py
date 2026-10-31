"""Pruefung direkt vor dem Senden (wie 'simulate before send'): Kosten und Kursdrift in R.
Und: Groesse nach Staerke des Vorteils (Bruchteil-Kelly-Gedanke), nie mehr als die Limits erlauben."""
from src.firm.money.entry_quality import edge_factor, entry_check

# BUY: Einstieg 100, Stop 99 (1 R = 1.0), Ziel 102 (2 R)


def test_good_entry_passes():
    assert entry_check("BUY", 100.0, 99.0, 102.0, bid=99.98, ask=100.02) is None


def test_spread_eating_the_risk_is_rejected():
    why = entry_check("BUY", 100.0, 99.0, 102.0, bid=99.8, ask=100.1)       # Spread 0.3 R
    assert why and "Spread" in why


def test_price_ran_away_is_rejected_no_chasing():
    why = entry_check("BUY", 100.0, 99.0, 102.0, bid=100.38, ask=100.4)     # 0.4 R schlechter als Signal
    assert why and "Kurs" in why
    assert entry_check("SELL", 100.0, 101.0, 98.0, bid=99.6, ask=99.62) and \
        entry_check("SELL", 100.0, 101.0, 98.0, bid=100.1, ask=100.12) is None   # SELL: hoeher = besser


def test_one_to_one_targets_are_allowed_but_reached_target_is_not():
    assert entry_check("BUY", 100.0, 99.0, 101.0, bid=99.95, ask=100.05) is None   # 1:1 ist eine gueltige Strategie
    why = entry_check("BUY", 100.0, 99.0, 100.2, bid=100.19, ask=100.21)         # Ziel schon da
    assert why and "Ziel" in why


def test_missing_quote_or_bad_levels_fail_closed():
    assert entry_check("BUY", 100.0, 99.0, 102.0, bid=None, ask=None)
    assert entry_check("BUY", 100.0, 100.0, 102.0, bid=99.99, ask=100.01)
    assert entry_check("BUY", 100.0, 101.0, 102.0, bid=99.99, ask=100.01)   # Stop auf der falschen Seite


def test_edge_factor_only_reduces():
    assert edge_factor(None) == 1.0
    assert edge_factor(0.40) == 1.0
    assert edge_factor(0.12) == 0.5
    assert edge_factor(-0.1) == 0.25
    assert all(edge_factor(x) <= 1.0 for x in (0.0, 0.05, 0.2, 5.0))


def test_backtest_uses_the_same_rule():
    from src.firm.money.entry_quality import backtest_entry_ok
    assert backtest_entry_ok(1.0, 2.0, 0.04)
    assert not backtest_entry_ok(1.0, 2.0, 0.3)            # Spread 0.3 R
    assert backtest_entry_ok(1.0, 1.0, 0.1)                # 1:1 erlaubt
    assert backtest_entry_ok(1.0, None, 0.1)               # ohne festes Ziel: nur Kostenregel
    assert not backtest_entry_ok(0.0, 2.0, 0.0)


def test_shadow_book_skips_trades_live_would_never_send():
    from src.firm.money.scanner import MarketScanner
    sc = MarketScanner.__new__(MarketScanner)
    sc.shadows = []
    hit = {"epic": "X", "strategy": "orb", "price": 100.0, "bar_ts": 1.0, "whitelisted": False}
    sc._open_shadow(hit, ("BUY", 1.0, 2.0), {"bid": 99.8, "ask": 100.1})    # Spread 0.3 R
    assert sc.shadows == []
    sc._open_shadow(hit, ("BUY", 1.0, 2.0), {"bid": 99.99, "ask": 100.01})
    assert len(sc.shadows) == 1
