import pandas as pd
import numpy as np
from typing import Dict, Any

def simple_trend_strategy(data: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """
    Example Strategy: Moving Average Crossover
    """
    fast_ma = data['close'].rolling(window=params.get('fast', 10)).mean()
    slow_ma = data['close'].rolling(window=params.get('slow', 30)).mean()

    signals = pd.Series(0, index=data.index)
    signals[fast_ma > slow_ma] = 1
    signals[fast_ma < slow_ma] = -1

    # We return the 'change' in signal to trigger trades
    return signals.diff().fillna(0).apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))

# Note: This is a simplified version. The actual strategy pipeline
# will be driven by the Quant Developer Agent.
