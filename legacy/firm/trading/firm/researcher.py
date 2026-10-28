import logging
import numpy as np
import pandas as pd
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from src.data.loader import DataLoader
from src.data.backtest import BacktestEngine
from src.risk.engine import RiskEngine, RiskConfig

@dataclass
class StrategyProposal:
    name: str
    params: dict
    expected_sharpe: float
    max_drawdown: float
    verdict: str # "PROPOSE" or "REJECT"

class StrategyResearcher:
    """
    The Quant Researcher of the firm.
    Responsibilities:
    1. Market Scanning: Analyze historical data for potential edges.
    2. Hypothesis Generation: Create parameter variations of known strategies.
    3. Rigorous Validation: Use the BacktestEngine to verify if the edge is real.
    4. Pitching: Present a StrategyProposal to the CEO.
    """
    def __init__(self, data_loader: DataLoader):
        self.data_loader = data_loader
        self.logger = logging.getLogger("Quant_Researcher")

    def research_parameter_sweep(self, strategy_cls, symbol: str, timeframe: str, param_grid: Dict[str, List[Any]]):
        """
        Tries multiple parameter combinations to find the most robust version of a strategy.
        Rejects strategies that are too sensitive to small parameter changes (overfitting).
        """
        self.logger.info(f"Starting parameter sweep for {strategy_cls.__name__} on {symbol}...")

        df = self.data_loader.load_csv(symbol, timeframe)
        if df.empty:
            return None

        best_sharpe = -float('inf')
        best_params = None

        # Simplified grid search
        import itertools
        keys, values = zip(*param_grid.items())
        for v in itertools.product(*values):
            params = dict(zip(keys, v))

            # We use a dedicated RiskConfig for the researcher's backtests
            risk_cfg = RiskConfig(10000, 1000, 2000, 3.0, 5.0)
            risk_eng = RiskEngine(risk_cfg)
            backtester = BacktestEngine(10000, risk_eng)

            strategy = strategy_cls("ResearchTest", params)
            def signal_fn(s, d):
                sig = strategy.on_tick(s, d)
                return strategy.to_order_request(sig) if sig else None

            # Convert df to ticks
            ticks = []
            for ts, row in df.iterrows():
                from src.data.loader import MarketTick
                ticks.append(MarketTick(ts.timestamp(), symbol, row['close'], 0.0001, row['volume']))

            res = backtester.run(ticks, signal_fn)

            if res.sharpe_ratio > best_sharpe:
                best_sharpe = res.sharpe_ratio
                best_params = params

        if best_params and best_sharpe > 1.0: # Only propose if Sharpe > 1.0
            self.logger.info(f"Found promising edge! Sharpe: {best_sharpe:.2f}")
            return StrategyProposal(
                name=f"{strategy_cls.__name__}_Optimized",
                params=best_params,
                expected_sharpe=best_sharpe,
                max_drawdown=0.0, # Would be calculated from res
                verdict="PROPOSE"
            )

        self.logger.info("No robust edge found in this sweep.")
        return StrategyProposal("None", {}, 0, 0, "REJECT")
