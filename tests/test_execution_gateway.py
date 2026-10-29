import asyncio
import pytest
from src.firm.money.execution_gateway import ExecutionGateway, TradeProposal
from src.firm.money.risk_gate import RiskGate
from src.firm.broker.client import CapitalComClient
from src.firm.bus.event_store import EventStore
from pathlib import Path

class StubBroker:
    def __init__(self):
        self.attempts = 0
    async def place_order(self, epic: str, side: str, size: float, stop_loss: float, take_profit=None):
        self.attempts += 1
        return {"dealId": "deal-1", "price": 100.0}
    async def get_account_details(self):
        return {"balance": 200.0, "open_positions": 0}
    async def authenticate(self):
        return True
    async def close(self):
        pass

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

@pytest.fixture
def event_store(tmp_path):
    return EventStore(str(tmp_path / "test_gateway.db"))

@pytest.mark.asyncio
async def test_execution_idempotency(risk_gate, event_store):
    broker = StubBroker()
    gateway = ExecutionGateway(broker, risk_gate, event_store=event_store)
    proposal = TradeProposal(
        epic="BTCUSD", side="BUY", size=0.005, stop_loss=99.0, reason="Test"
    )

    # First execution
    res1 = await gateway.execute(proposal, 200.0, 0, current_price=100.0)
    assert res1["status"] == "FILLED"

    # Second execution with same proposal_id
    res2 = await gateway.execute(proposal, 200.0, 0, current_price=100.0)
    assert res2["status"] == "REJECTED"
    assert "Duplicate" in res2["reason"]

@pytest.mark.asyncio
async def test_execution_risk_rejection(risk_gate, event_store):
    gateway = ExecutionGateway(StubBroker(), risk_gate, event_store=event_store)
    # Proposal with invalid stop loss
    proposal = TradeProposal(
        epic="BTCUSD", side="BUY", size=0.005, stop_loss=-1, reason="Bad SL"
    )

    res = await gateway.execute(proposal, 200.0, 0, current_price=100.0)
    assert res["status"] == "REJECTED"
    assert "Stop-Loss" in res["reason"]

@pytest.mark.asyncio
async def test_execution_max_positions(risk_gate, event_store):
    gateway = ExecutionGateway(StubBroker(), risk_gate, event_store=event_store)
    proposal = TradeProposal(
        epic="BTCUSD", side="BUY", size=0.005, stop_loss=99.0, reason="Limit test"
    )

    # 3 positions already open
    res = await gateway.execute(proposal, 200.0, 3, current_price=100.0)
    assert res["status"] == "REJECTED"
    assert "Max positions" in res["reason"]


@pytest.mark.asyncio
async def test_live_mode_requires_approval_token(risk_gate, event_store, tmp_path, monkeypatch):
    import json
    import time
    from src.firm.money.live_approval import issue
    env = {"CAPITAL_LIVE_API_KEY": "lk", "CAPITAL_LIVE_PASSWORD": "lp", "CAPITAL_LIVE_IDENTIFIER": "me@x"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    token_path = tmp_path / "live_token.json"
    gateway = ExecutionGateway(
        StubBroker(), risk_gate, event_store=event_store,
        mode="LIVE_MICRO", live_token_path=str(token_path),
    )
    proposal = TradeProposal(
        epic="BTCUSD", side="BUY", size=0.005, stop_loss=99.0, reason="Live test"
    )

    blocked = await gateway.execute(proposal, 200.0, 0, current_price=100.0)
    assert blocked["status"] == "REJECTED"
    assert "Live approval token" in blocked["reason"]

    # An unsigned token (e.g. written by another process) does not unlock anything
    token_path.write_text(json.dumps({"expires_at": time.time() + 60}), encoding="utf-8")
    forged = await gateway.execute(TradeProposal(epic="BTCUSD", side="BUY", size=0.005, stop_loss=99.0,
                                                 reason="forged"), 200.0, 0, current_price=100.0)
    assert forged["status"] == "REJECTED"

    # A valid, signed, unexpired token unlocks live execution
    token_path.write_text(json.dumps(issue(60, env, risk_gate.limits_path)), encoding="utf-8")
    allowed = await gateway.execute(proposal, 200.0, 0, current_price=100.0)
    assert allowed["status"] == "FILLED"

    # An expired token blocks again (signed, but in the past)
    token_path.write_text(json.dumps(issue(60, env, risk_gate.limits_path, now=time.time() - 120)), encoding="utf-8")
    proposal2 = TradeProposal(
        epic="BTCUSD", side="BUY", size=0.005, stop_loss=99.0, reason="Live test 2"
    )
    expired = await gateway.execute(proposal2, 200.0, 0, current_price=100.0)
    assert expired["status"] == "REJECTED"
    assert "Live approval token" in expired["reason"]



def test_live_token_bound_to_limits_and_account_and_tamper_proof(tmp_path):
    import time
    from src.firm.money.live_approval import issue, verify
    limits = tmp_path / "limits.toml"
    limits.write_text("[trade]\nmax_risk_per_trade = 0.005\n")
    env = {"CAPITAL_LIVE_API_KEY": "lk", "CAPITAL_LIVE_PASSWORD": "lp", "CAPITAL_LIVE_IDENTIFIER": "me@x"}
    tok = issue(3600, env, limits)
    assert verify(tok, env, limits)[0]
    assert not verify(dict(tok, expires_at=time.time() + 10**9), env, limits)[0]      # tampered
    assert not verify(tok, dict(env, CAPITAL_LIVE_IDENTIFIER="other@x"), limits)[0]   # other account
    assert not verify(tok, {}, limits)[0]                                             # brain zone: no keys
    limits.write_text("[trade]\nmax_risk_per_trade = 0.02\n")
    assert "Limits" in verify(tok, env, limits)[1]                                    # limits changed
