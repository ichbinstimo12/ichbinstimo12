import tomllib
import logging
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RiskGate")

@dataclass
class TradeProposal:
    epic: str
    side: str  # "BUY" or "SELL"
    size: float
    stop_loss: float
    take_profit: Optional[float] = None
    reason: str = ""

class RiskGate:
    """
    The ultimate firewall. Validates every TradeProposal against limits.toml
    and real-time account data.
    """
    def __init__(self, limits_path: str, whitelist_path: str):
        self.limits_path = Path(limits_path)
        self.whitelist_path = Path(whitelist_path)
        self.limits = self._load_limits()
        self.whitelist = self._load_whitelist()

    def _load_limits(self) -> Dict[str, Any]:
        with open(self.limits_path, "rb") as f:
            return tomllib.load(f)

    def _load_whitelist(self) -> Dict[str, Any]:
        if not self.whitelist_path.exists():
            logger.warning(f"Whitelist file {self.whitelist_path} missing. Trading disabled.")
            return {"allowed_epics": []}
        with open(self.whitelist_path, "rb") as f:
            return tomllib.load(f)

    def validate(self, proposal: TradeProposal, account_balance: float, current_positions: int,
                 current_price: Optional[float] = None, fx_to_account: float = 1.0) -> Tuple[bool, str]:
        """
        Validates a trade proposal. Returns (is_allowed, reason).

        `fx_to_account` converts one unit of the instrument's quote currency
        into account currency. The risk check must compare like with like:
        stop distance x size is in quote currency (USD for BTCUSD/GOLD), the
        budget is in account currency (CHF). Without the conversion every USD
        trade on a CHF account was rejected as "risk too high".
        """
        # 1. Whitelist Check
        if proposal.epic not in self.whitelist.get("allowed_epics", []):
            return False, f"Epic {proposal.epic} not in approved whitelist"

        # 2. Max Positions Check
        if current_positions >= self.limits["account"]["max_positions"]:
            return False, f"Max positions ({self.limits['account']['max_positions']}) reached"

        # 3. Stop-Loss Validation
        if proposal.stop_loss <= 0:
            return False, "Stop-Loss is mandatory and must be positive"

        if current_price is None:
            # Without a live price we cannot judge the risk geometry.
            return False, "Current price unavailable - trade rejected"

        # Stop-Loss must sit on the opposite side of the entry price.
        if (proposal.side == "BUY" and proposal.stop_loss >= current_price) or \
           (proposal.side == "SELL" and proposal.stop_loss <= current_price):
            return False, "Stop-Loss must be placed on the wrong side of the current price"

        # 4. Trade Risk Check (max_risk_per_trade of the account balance)
        if not fx_to_account or fx_to_account <= 0:
            return False, "Exchange rate to account currency unknown - trade rejected"
        risk_amount = abs(current_price - proposal.stop_loss) * proposal.size * fx_to_account
        # Tiny tolerance for rounding of stop levels sent to the broker.
        max_allowed_risk = account_balance * self.limits["trade"]["max_risk_per_trade"] * 1.001

        if risk_amount > max_allowed_risk:
            return False, f"Risk too high: {risk_amount:.2f} exceeds limit {max_allowed_risk:.2f}"

        return True, "Approved"

    def check_account_health(self, current_balance: float, peak_balance: float, daily_pnl: float,
                             day_start: Optional[float] = None) -> Tuple[bool, str]:
        """
        Checks for Daily Loss and Drawdown limits. Pass equity (incl. open P&L) as `current_balance`.
        The daily loss is measured against the day's starting value when known.
        """
        if not peak_balance or peak_balance <= 0:
            return False, "Peak balance unknown - not trading"
        base = day_start if day_start and day_start > 0 else current_balance
        if daily_pnl <= -(base * self.limits["account"]["daily_loss_limit"]):
            return False, "Daily loss limit exceeded"

        # Max Drawdown Limit (8%)
        drawdown = (peak_balance - current_balance) / peak_balance
        if drawdown >= self.limits["account"]["max_drawdown_limit"]:
            return False, "Max drawdown limit exceeded"

        return True, "Healthy"
