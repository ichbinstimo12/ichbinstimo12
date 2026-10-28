import asyncio
import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MockBrokerClient")

@dataclass
class BrokerSession:
    cst: str
    x_security_token: str
    expires_at: float

class MockBrokerClient:
    """
    Simulates Capital.com API responses using realistic market data
    for a small account feasibility check.
    """
    def __init__(self, api_key: str, identifier: str, password: str):
        self.api_key = api_key
        self.identifier = identifier
        self.password = password
        self.session: Optional[BrokerSession] = None

        # Realistic market data for common instruments
        # spread: distance between bid/ask in points
        # min_order_size: smallest allowed trade (e.g., 0.1 lot)
        # overnight_fee: estimated daily cost per min order
        self.mock_markets = [
            {
                "epic": "BTCUSD",
                "name": "Bitcoin",
                "spread": 15.0,            # $15 spread on BTC
                "min_order_size": 0.01,    # 0.01 BTC
                "overnight_fee": 0.50,
            },
            {
                "epic": "EURUSD",
                "name": "EUR/USD",
                "spread": 0.0001,          # 1 pip
                "min_order_size": 1000,     # 1k units
                "overnight_fee": 0.05,
            },
            {
                "epic": "US500",
                "name": "S&P 500",
                "spread": 1.2,             # $1.2 spread
                "min_order_size": 0.1,     # 0.1 lot
                "overnight_fee": 0.20,
            },
            {
                "epic": "XAUUSD",
                "name": "Gold",
                "spread": 0.30,            # $0.30 spread
                "min_order_size": 0.01,
                "overnight_fee": 0.15,
            }
        ]

    async def authenticate(self) -> bool:
        logger.info("[MOCK] Authenticating...")
        self.session = BrokerSession(
            cst="mock-cst-123",
            x_security_token="mock-token-abc",
            expires_at=0.0
        )
        return True

    async def get_account_details(self) -> Dict[str, Any]:
        return {"balance": 200.0, "currency": "CHF", "equity": 200.0}

    async def get_positions(self) -> List[Dict[str, Any]]:
        return []

    async def get_market_list(self) -> List[Dict[str, Any]]:
        logger.info("[MOCK] Fetching market list...")
        return self.mock_markets

    async def get_historical_prices(self, epic: str, timeframe: str = "MINUTE", limit: int = 100) -> List[Dict[str, Any]]:
        return [{"price": 60000.0, "ts": "2026-10-23T10:00:00Z"}]

    async def close(self):
        pass
