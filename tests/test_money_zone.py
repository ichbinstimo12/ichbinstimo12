import pytest
import asyncio
from src.firm.money.risk_gate import RiskGate, TradeProposal
from src.firm.money.kill_switch import KillSwitch
from src.firm.broker.client import CapitalComClient
from pathlib import Path

@pytest.fixture
def risk_gate(tmp_path):
    limits_file = tmp_path / "limits.toml"
    limits_file.write_text("""
[account]
daily_loss_limit = 0.02
max_drawdown_limit = 0.08
max_leverage = 10.0
max_positions = 3

[trade]
max_risk_per_trade = 0.005
max_pos_size_per_instrument = 0.1

[operational]
max_token_budget_daily = 1000000
max_agents = 10
""")
    whitelist_file = tmp_path / "whitelist.toml"
    whitelist_file.write_text("""
allowed_epics = ["BTCUSD"]
""")
    return RiskGate(str(limits_file), str(whitelist_file))

def test_risk_gate_max_positions(risk_gate):
    proposal = TradeProposal("BTCUSD", "BUY", 0.1, 55000, "BTC buy test")
    allowed, reason = risk_gate.validate(proposal, 200.0, 3) # 3 positions open
    assert not allowed
    assert "Max positions" in reason

def test_risk_gate_no_stop_loss(risk_gate):
    proposal = TradeProposal("BTCUSD", "BUY", 0.1, -1, "No SL test")
    allowed, reason = risk_gate.validate(proposal, 200.0, 0)
    assert not allowed
    assert "Stop-Loss is mandatory" in reason

def test_account_health_daily_loss(risk_gate):
    # Balance 200, Daily Loss Limit 2% = 4 CHF
    # PnL = -5 CHF
    allowed, reason = risk_gate.check_account_health(200.0, 200.0, -5.0)
    assert not allowed
    assert "Daily loss limit exceeded" in reason

def test_account_health_drawdown(risk_gate):
    # Balance 180, Peak 200. DD = 20/200 = 10% (Limit 8%)
    allowed, reason = risk_gate.check_account_health(180.0, 200.0, 0.0)
    assert not allowed
    assert "Max drawdown limit exceeded" in reason

@pytest.mark.asyncio
async def test_killswitch_integration():
    # Mock Broker
    class MockBroker:
        def __init__(self):
            self.positions = 1
        async def flatten_all(self):
            self.positions = 0
        async def get_positions(self):
            return [{"dealId": "x", "epic": "BTCUSD", "direction": "BUY", "volume": 1}] * self.positions
        async def get_account_details(self):
            return {"balance": 1000.0}
        async def authenticate(self): return True
        async def close(self): pass

    broker = MockBroker()
    ks = KillSwitch(broker)
    success = await ks.activate()
    assert success
    assert broker.positions == 0
