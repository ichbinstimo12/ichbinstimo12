from src.firm.money.limits_check import limit_warnings


def test_current_style_limits_are_consistent():
    assert limit_warnings({"trade": {"max_risk_per_trade": 0.005, "max_total_risk": 0.015},
                           "account": {"daily_loss_limit": 0.02, "max_drawdown_limit": 0.08}}) == []


def test_raising_risk_per_trade_alone_is_flagged():
    w = limit_warnings({"trade": {"max_risk_per_trade": 0.02},
                        "account": {"daily_loss_limit": 0.02, "max_drawdown_limit": 0.08}})
    assert any("Klumpenrisiko" in x for x in w) and any("EIN Stop-Treffer" in x for x in w)


def test_two_percent_with_matching_limits_is_consistent():
    assert limit_warnings({"trade": {"max_risk_per_trade": 0.02, "max_total_risk": 0.04},
                           "account": {"daily_loss_limit": 0.045, "max_drawdown_limit": 0.08}}) == []
