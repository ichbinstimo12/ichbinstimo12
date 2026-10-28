import asyncio
import logging
from typing import Any, Optional
from src.firm.broker.client import CapitalComClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("KillSwitch")

class KillSwitch:
    """
    High-priority interrupt to flatten all positions and stop agents.
    """
    def __init__(self, broker: CapitalComClient):
        self.broker = broker
        self.is_active = False

    async def activate(self):
        """
        Triggers the flatten-all process with aggressive retries.
        """
        logger.critical("KILL-SWITCH ACTIVATED: Flattening all positions...")
        self.is_active = True

        success = False
        attempts = 0
        max_attempts = 10

        while not success and attempts < max_attempts:
            attempts += 1
            try:
                # 1. Attempt to close all positions
                # This is a pseudo-call; we'll implement flatten_all in BrokerClient
                if hasattr(self.broker, 'flatten_all'):
                    await self.broker.flatten_all()
                elif hasattr(self.broker, 'close_position'):
                    for pos in await self.broker.get_positions():
                        if pos.get("dealId"):
                            await self.broker.close_position(str(pos["dealId"]))
                else:
                    raise RuntimeError("broker can neither flatten nor close positions")

                # 2. Verify with the broker: open positions must really be gone.
                positions = await self.broker.get_positions()
                if not positions:
                    logger.info("All positions closed successfully.")
                    success = True
                else:
                    logger.warning(f"{len(positions)} position(s) still open. Attempt {attempts}/{max_attempts}")

            except Exception as e:
                logger.error(f"Error during flatten: {e}")
                # If token expired, re-authenticate
                if "401" in str(e):
                    logger.info("Session expired during kill. Re-authenticating...")
                    await self.broker.authenticate()

                await asyncio.sleep(2)

        if not success:
            logger.critical("KILL-SWITCH FAILED: Unable to flatten all positions!")

        return success
