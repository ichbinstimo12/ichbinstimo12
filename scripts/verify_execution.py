import asyncio
import logging
from src.firm.broker.client import CapitalComClient
from src.firm.money.risk_gate import RiskGate
from src.firm.money.execution_gateway import ExecutionGateway, TradeProposal
from pathlib import Path

# Setup logging to see the flow clearly
logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ManualVerification")

async def run_verification():
    print("\n=== STARTING MANUAL EXECUTION VERIFICATION ===\n")

    # 1. Setup Environment
    # We use a temporary limits file for this test
    limits_path = "config/limits.toml"
    whitelist_path = "config/whitelist.toml"

    # Create a dummy whitelist file if it doesn't exist
    Path(whitelist_path).touch()

    broker = CapitalComClient("mock_key", "mock_id", "mock_pass")
    risk_gate = RiskGate(limits_path, whitelist_path)
    gateway = ExecutionGateway(broker, risk_gate)

    # SCENARIO 1: Valid Trade
    print("\n--- Scenario 1: Valid Trade Proposal ---")
    proposal_1 = TradeProposal(
        epic="BTCUSD",
        side="BUY",
        size=0.01,
        stop_loss=58000.0,
        reason="Technical breakout observed"
    )
    res1 = await gateway.execute(proposal_1, account_balance=200.0, current_positions=0)
    print(f"Result: {res1}")

    # SCENARIO 2: Risk Violation (Max Positions)
    print("\n--- Scenario 2: Risk Violation (Max Positions) ---")
    proposal_2 = TradeProposal(
        epic="EURUSD",
        side="BUY",
        size=1000,
        stop_loss=1.08,
        reason="Trend following"
    )
    # Simulate 3 positions already open (Limit is 3)
    res2 = await gateway.execute(proposal_2, account_balance=200.0, current_positions=3)
    print(f"Result: {res2}")

    # SCENARIO 3: Idempotency (Duplicate Proposal)
    print("\n--- Scenario 3: Idempotency Check ---")
    # Use the same proposal object as Scenario 1
    res3 = await gateway.execute(proposal_1, account_balance=200.0, current_positions=0)
    print(f"Result: {res3}")

    # SCENARIO 4: Mandatory Stop-Loss check
    print("\n--- Scenario 4: Missing/Invalid Stop-Loss ---")
    proposal_4 = TradeProposal(
        epic="XAUUSD",
        side="BUY",
        size=0.01,
        stop_loss=-1.0, # Invalid SL
        reason="Trying to bypass SL"
    )
    res4 = await gateway.execute(proposal_4, account_balance=200.0, current_positions=0)
    print(f"Result: {res4}")

    print("\n=== VERIFICATION COMPLETE ===\n")

if __name__ == "__main__":
    asyncio.run(run_verification())
