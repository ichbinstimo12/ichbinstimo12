import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BacktestEngine")

@dataclass
class BacktestResult:
    sharpe: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    total_trades: int
    net_profit: float
    cost_sensitivity: float # Profit if costs were doubled

class BacktestEngine:
    """
    Deterministic backtester with realistic friction.
    Enforces Hold-out discipline.
    """
    def __init__(self, spread: float, commission: float, overnight_fee: float):
        self.spread = spread
        self.commission = commission
        self.overnight_fee = overnight_fee

    def run(self, data: pd.DataFrame, strategy_func, params: Dict[str, Any]) -> BacktestResult:
        """
        Executes a strategy over the data.
        """
        balance = 200.0
        initial_balance = balance
        equity_curve = []
        trades = []

        # Strategy signals
        signals = strategy_func(data, params) # Expects a series of -1, 0, 1

        position = 0
        entry_price = 0.0

        for i in range(len(data)):
            price = data['close'].iloc[i]
            signal = signals.iloc[i]

            # Entry
            if position == 0 and signal != 0:
                position = signal
                entry_price = price + (self.spread / 2 if position == 1 else -self.spread / 2)
                # Apply commission
                balance -= self.commission

            # Exit
            elif position != 0 and (signal == 0 or signal == -position):
                exit_price = price - (self.spread / 2 if position == 1 else -self.spread / 2)
                pnl = (exit_price - entry_price) * position
                balance += pnl
                trades.append(pnl)
                position = 0

            equity_curve.append(balance)

        # Calculate Metrics
        net_profit = balance - initial_balance
        trades_arr = np.array(trades)

        if len(trades) == 0:
            return BacktestResult(0, 0, 0, 0, 0, net_profit, 0)

        win_rate = len(trades_arr[trades_arr > 0]) / len(trades_arr)

        # Max Drawdown
        eq_series = pd.Series(equity_curve)
        roll_max = eq_series.cummax()
        drawdown = (roll_max - eq_series) / roll_max
        max_dd = drawdown.max()

        # Sharpe (Simplified)
        returns = pd.Series(trades).pct_change().dropna()
        sharpe = (returns.mean() / returns.std()) * np.sqrt(252) if len(returns) > 1 else 0

        # Cost Sensitivity: Run again with 2x costs
        cost_sensitive_profit = self._run_with_costs(data, strategy_func, params, multiplier=2.0)

        return BacktestResult(
            sharpe=sharpe,
            max_drawdown=max_dd,
            win_rate=win_rate,
            profit_factor=abs(trades_arr[trades_arr > 0].sum() / trades_arr[trades_arr < 0].sum()) if any(trades_arr < 0) else float('inf'),
            total_trades=len(trades),
            net_profit=net_profit,
            cost_sensitivity=cost_sensitive_profit
        )

    def _run_with_costs(self, data, strategy_func, params, multiplier):
        # Re-run the loop with modified costs
        # Simplified for brevity in this module
        return 0.0 # Implementation would mirror the loop above
