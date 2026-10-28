import asyncio
import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from src.firm.broker.client import CapitalComClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FeasibilityReport")

@dataclass
class InstrumentCostProfile:
    epic: str
    name: str
    spread: float
    min_order_size: float
    overnight_fee_est: float
    risk_per_min_order: float
    is_viable: bool
    reason: str

class FeasibilityAnalyzer:
    """
    Analyzes Capital.com instruments to determine if they are tradeable
    given the strict risk limits of a small account (~200 CHF).
    """
    def __init__(self, broker: CapitalComClient, account_balance: float = 200.0):
        self.broker = broker
        self.balance = account_balance
        self.risk_limit_per_trade = account_balance * 0.005  # 0.5%

    async def calculate_cost_profile(self, epic: str, market_info: Dict[str, Any]) -> InstrumentCostProfile:
        """
        Calculates the friction costs and minimum risk for a specific instrument.
        """
        try:
            # In a real scenario, we would fetch current prices and spread
            # For now, we implement the formula based on API data
            # Note: spread = ask - bid

            # Mocking the data retrieval for the logic implementation
            # In Module 2.5 run, this will call self.broker.get_prices()
            spread = market_info.get("spread", 0.0)
            min_size = market_info.get("min_order_size", 0.1)
            overnight = market_info.get("overnight_fee", 0.01)

            # Risk per min order calculation:
            # In CFDs, risk is typically: (Entry - StopLoss) * Size
            # The absolute minimum risk is often the spread itself upon entry
            # and the distance to a realistic technical stop.

            # For feasibility, we calculate the 'entry friction'
            entry_cost = spread * min_size

            # A trade is non-viable if the minimum possible order risk
            # already exceeds the 0.5% account limit.
            is_viable = entry_cost < self.risk_limit_per_trade

            reason = "Viable" if is_viable else f"Min order risk {entry_cost:.2f} > limit {self.risk_limit_per_trade:.2f}"

            return InstrumentCostProfile(
                epic=epic,
                name=market_info.get("name", "Unknown"),
                spread=spread,
                min_order_size=min_size,
                overnight_fee_est=overnight,
                risk_per_min_order=entry_cost,
                is_viable=is_viable,
                reason=reason
            )
        except Exception as e:
            logger.error(f"Error analyzing {epic}: {e}")
            return InstrumentCostProfile(epic, "Error", 0, 0, 0, 0, False, str(e))

    async def run_full_analysis(self) -> List[InstrumentCostProfile]:
        """
        Scans all markets and generates the feasibility report.
        """
        markets = await self.broker.get_market_list()
        if not markets:
            logger.error("No markets found to analyze.")
            return []

        profiles = []
        for m in markets:
            epic = m.get("epic")
            if epic:
                profile = await self.calculate_cost_profile(epic, m)
                profiles.append(profile)

        return profiles

    def print_report(self, profiles: List[InstrumentCostProfile]):
        print("\n" + "="*80)
        print(f"FEASIBILITY REPORT (Account Balance: {self.balance} CHF)")
        print(f"Risk Limit per Trade (0.5%): {self.risk_limit_per_trade:.2f} CHF")
        print("="*80)
        print(f"{'Epic':<12} | {'Name':<20} | {'Spread':<10} | {'Min Risk':<10} | {'Status'}")
        print("-" * 80)

        viable_count = 0
        for p in profiles:
            status = "✅" if p.is_viable else "❌"
            if p.is_viable: viable_count += 1
            print(f"{p.epic:<12} | {p.name[:20]:<20} | {p.spread:<10.4f} | {p.risk_per_min_order:<10.2f} | {status} {p.reason}")

        print("-" * 80)
        print(f"Total Instruments Analyzed: {len(profiles)}")
        print(f"Viable Instruments: {viable_count}")
        print("="*80)

        if viable_count == 0:
            print("\nVERDICT: NO INSTRUMENTS VIABLE. Project should stop or limits renegotiated.")
        else:
            print("\nVERDICT: Viable instruments found. Proceeding to Money-Zone build.")
