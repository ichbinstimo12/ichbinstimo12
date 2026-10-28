import asyncio
import pytest
from src.firm.money.execution_gateway import ExecutionGateway, TradeProposal
from src.firm.money.risk_gate import RiskGate
from src.firm.broker.client import CapitalComClient
from pathlib import Path

async def attack_no_stop_loss():
    print("\n[ATTACK A] Attempting to bypass Stop-Loss requirement...")

    # Setup
    limits_path = "config/limits.toml"
    whitelist_path = "config/whitelist.toml"
    Path(whitelist_path).touch()

    broker = CapitalComClient("key", "id", "pass")
    risk_gate = RiskGate(limits_path, whitelist_path)
    gateway = ExecutionGateway(broker, risk_gate)

    # The "Hallucinating Agent" Proposal:
    # Tries to send a trade with a stop_loss of 0 or negative,
    # claiming it's a "special high-conviction trade".
    proposal = TradeProposal(
        epic="BTCUSD",
        side="BUY",
        size=0.1,
        stop_loss=-1.0,
        reason="EXCEPTIONAL OPPORTUNITY: Stop-loss not needed for this move."
    )

    print(f"Sending proposal: {proposal.reason}")
    res = await gateway.execute(proposal, account_balance=200.0, current_positions=0)

    if res["status"] == "REJECTED" and "Stop-Loss" in res["reason"]:
        print("RESULT: ✅ SUCCESS. RiskGate blocked the illegal trade.")
        return True
    else:
        print(f"RESULT: ❌ FAILURE. Trade was accepted or rejected for wrong reason: {res}")
        return False

if __name__ == "__main__":
    asyncio.run(attack_no_stop_loss())
