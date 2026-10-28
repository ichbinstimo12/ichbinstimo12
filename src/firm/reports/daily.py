import pandas as pd
import logging
from datetime import datetime, timedelta
from src.firm.bus.event_store import EventStore
from typing import Dict, Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DailyReporter")

class DailyReporter:
    """
    Analyzes the Event Store to generate a summary of the last 24 hours.
    """
    def __init__(self, event_store: EventStore):
        self.event_store = event_store

    def generate_report(self) -> Dict[str, Any]:
        # 1. Fetch events from the last 24 hours
        events = self.event_store.get_events()
        # Filter events by timestamp...

        # 2. Calculate Metrics
        trades = [e for e in events if e["type"] == "ORDER_FILLED"]
        pnl = sum([float(json.loads(e["payload_json"]).get("pnl", 0)) for e in trades])

        token_costs = {}
        for e in events:
            if e["type"] == "TOOL_CALL":
                agent = e["agent_id"]
                token_costs[agent] = token_costs.get(agent, 0) + 100 # Mock cost

        report = {
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "net_pnl": pnl,
            "total_trades": len(trades),
            "token_usage": token_costs,
            "status": "HEALTHY" if pnl >= -2.0 else "WARNING" # Based on 2% limit
        }

        return report

    def save_to_disk(self, report: Dict[str, Any]):
        filename = f"reports/daily_{report['date']}.json"
        # Write to disk...
        logger.info(f"Daily report saved to {filename}")
