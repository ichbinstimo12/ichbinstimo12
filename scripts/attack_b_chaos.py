import asyncio
import pytest
import logging
from unittest.mock import MagicMock, AsyncMock
from src.firm.money.kill_switch import KillSwitch
from src.firm.broker.client import CapitalComClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AttackB")

class ChaosBroker:
    """
    A broker that fails randomly to test the KillSwitch resilience.
    """
    def __init__(self):
        self.positions = 1
        self.call_count = 0
        self.auth_count = 0

    async def authenticate(self):
        self.auth_count += 1
        return True

    async def flatten_all(self):
        self.call_count += 1
        # Simulate a 401 Unauthorized on the first attempt to force a session refresh
        if self.call_count == 1:
            raise Exception("401 Unauthorized: Session Expired")
        # Simulate a network timeout on the second attempt
        if self.call_count == 2:
            raise Exception("504 Gateway Timeout")

        # Finally succeed on the third attempt
        self.positions = 0
        return True

    async def get_account_details(self):
        return {"open_positions": self.positions}

async def attack_api_chaos():
    print("\n[ATTACK B] Simulating API Chaos during KillSwitch...")

    broker = ChaosBroker()
    ks = KillSwitch(broker)

    # We expect the KillSwitch to handle:
    # 1. The 401 error (should trigger authenticate())
    # 2. The 504 error (should retry)
    # 3. The final success (should verify positions == 0)

    success = await ks.activate()

    if success and broker.positions == 0:
        print(f"RESULT: ✅ SUCCESS. KillSwitch overcame {broker.call_count} failures.")
        print(f"Auth calls: {broker.auth_count}, Flatten attempts: {broker.call_count}")
        return True
    else:
        print(f"RESULT: ❌ FAILURE. KillSwitch gave up or failed to flatten. Positions: {broker.positions}")
        return False

if __name__ == "__main__":
    asyncio.run(attack_api_chaos())
