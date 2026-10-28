import logging
from typing import Dict, List, Any
from dataclasses import dataclass
from src.core.event_loop import TradingAgent
from src.risk.engine import RiskEngine, RiskConfig

@dataclass
class AgentPerformance:
    agent_id: str
    total_pnl: float
    sharpe: float
    current_allocation: float

class FirmManager:
    """
    The CEO of the Quant Firm.
    Responsibilities:
    1. Resource Allocation: Distributes total capital among active TradingAgents.
    2. Performance Review: Tracks which agents are profitable.
    3. Systemic Oversight: Can halt any agent or the entire firm.
    """
    def __init__(self, total_firm_capital: float):
        self.total_capital = total_firm_capital
        self.agents: Dict[str, TradingAgent] = {}
        self.performance_history: Dict[str, AgentPerformance] = {}
        self.logger = logging.getLogger("CEO_FirmManager")

    def hire_agent(self, agent_id: str, agent: TradingAgent, initial_allocation: float):
        """Onboards a new trader to the firm."""
        if initial_allocation > self.total_capital:
            self.logger.error(f"Insufficient capital to hire {agent_id}")
            return False

        self.agents[agent_id] = agent
        self.performance_history[agent_id] = AgentPerformance(
            agent_id=agent_id,
            total_pnl=0.0,
            sharpe=0.0,
            current_allocation=initial_allocation
        )
        self.logger.info(f"Hired {agent_id} with allocation ${initial_allocation}")
        return True

    def rebalance_capital(self):
        """
        The 'Performance Review' logic.
        Moves capital from underperforming agents to winners.
        """
        self.logger.info("Performing quarterly capital rebalance...")
        # Simplified Logic: If an agent has positive PnL, increase its allocation
        # In production, this uses a Kelly-inspired allocation based on Sharpe ratio.
        for agent_id, perf in self.performance_history.items():
            if perf.total_pnl > 0:
                # Reward the winner
                bonus = perf.total_pnl * 0.1 # Allocate 10% of profit back into the strategy
                perf.current_allocation += bonus
                self.logger.info(f"Promoting {agent_id}: New allocation ${perf.current_allocation:.2f}")

    def get_firm_status(self) -> Dict[str, Any]:
        """Returns data for the Command Center dashboard."""
        return {
            "total_equity": self.total_capital,
            "active_workers": list(self.agents.keys()),
            "performance": {k: v.__dict__ for k, v in self.performance_history.items()}
        }

    def emergency_halt_all(self):
        """The ultimate Kill Switch."""
        self.logger.critical("CEO ORDER: EMERGENCY HALT ALL TRADING")
        for agent in self.agents.values():
            agent.is_running = False
            agent.venue.flatten_all()
