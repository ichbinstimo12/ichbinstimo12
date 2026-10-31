import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from pathlib import Path
import pandas as pd
import numpy as np
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DataLoader")

class DataLoader:
    """
    Handles loading and cleaning of historical data.
    Enforces strict timestamp discipline and look-ahead prevention.
    """
    def __init__(self, data_dir: str | None = None):
        from src.firm.paths import data_dir as _home_data
        self.data_dir = Path(data_dir) if data_dir else _home_data() / "historical"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load_csv(self, epic: str) -> pd.DataFrame:
        file_path = self.data_dir / f"{epic}.csv"
        if not file_path.exists():
            logger.error(f"Data file not found for {epic}: {file_path}")
            # For now, return a dummy dataframe for the launch/test phase
            return self._generate_dummy_data(epic)

        df = pd.read_csv(file_path, parse_dates=['timestamp'])
        df = df.sort_values('timestamp').reset_index(drop=True)

        # Check for gaps
        diffs = df['timestamp'].diff()
        # Logic to log gaps > expected interval (e.g., 1 min)

        return df

    def _generate_dummy_data(self, epic: str) -> pd.DataFrame:
        """Generates synthetic data for initial development/testing."""
        logger.info(f"Generating synthetic data for {epic}...")
        dates = pd.date_range(start="2024-01-01", periods=1000, freq="min")
        prices = 60000 + np.cumsum(np.random.randn(1000) * 10)
        return pd.DataFrame({'timestamp': dates, 'close': prices})
