"""Memecoin-Desk: PAPER-Fills mit Kosten und die Exit-Leiter (Free Ride, Stufen, Trailing, Stops)."""
import asyncio

import pytest

from src.firm.memedesk import logic, paper

CFG = logic.load_config()
LIM = logic.load_limits()


def pos(**kw):
    return {"cost_sol": 1.0, "tokens_raw": 1000.0, "opened_at": 0.0, "peak_value": 1.0, **kw}


def test_hard_stop_at_minus_30():
    assert paper.exit_decision(pos(), 0.69, 10, CFG, LIM)[0].startswith("harter Stop")
    assert paper.exit_decision(pos(), 0.75, 10, CFG, LIM) is None


def test_free_ride_takes_the_stake_out_then_tiers_then_trailing():
    p = pos()
    reason, frac = paper.exit_decision(p, 2.0, 10, CFG, LIM)
    assert reason.startswith("Free Ride") and frac == pytest.approx(0.5)      # sell 1.0 of 2.0 = stake back
    assert not paper.apply_exit(p, reason, frac, 1.0) and p["free_ride"] and p["realized_sol"] == 1.0
    paper.mark(p, 1.0, 20, 10)
    assert paper.exit_decision(p, 1.0, 20, CFG, LIM) is None                  # free ride: no hard stop anymore
    reason, frac = paper.exit_decision(p, 2.1, 30, CFG, LIM)                   # total 3.1 = +210 %
    assert reason.startswith("Stufe +200") and frac == 0.25
    paper.apply_exit(p, reason, frac, 0.5)
    paper.mark(p, 2.4, 40, 10)
    reason, frac = paper.exit_decision(p, 1.6, 50, CFG, LIM)                   # -33 % from peak 2.4
    assert reason.startswith("Trailing") and frac == 1.0


def test_time_stop_and_immediate_exits():
    p = pos(last_move_at=0.0)
    assert paper.exit_decision(p, 1.02, CFG["exits"]["time_stop_minutes"] * 60 + 1, CFG, LIM)[0].startswith("Zeit-Stop")
    assert paper.exit_decision(p, 1.5, 10, CFG, LIM, alerts=["Dev verkauft"])[0].startswith("sofort raus")


def test_paper_fill_is_worse_than_the_quote():
    class Src:
        async def quote(self, i, o, amount, slippage_bps=300):
            return {"in": amount, "out": 1_000_000, "impact_pct": 2.0, "route": ["Pump.fun Amm"]}
    fill = asyncio.run(paper.paper_buy(Src(), "M", 0.15, CFG))
    assert fill["tokens_raw"] < 1_000_000 and fill["cost_sol"] > 0.15 and fill["slippage_bps"] == 500


def test_live_executor_is_not_built_yet():
    with pytest.raises(NotImplementedError):
        paper.LiveExecutor()
