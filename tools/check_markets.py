"""Read-only probe: which candidate epics are tradeable right now, and their rules.

Run: python -m tools.check_markets [EPIC ...]
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

from src.firm.broker.client import CapitalComClient

CANDIDATES = [
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "ADAUSD", "DOGEUSD", "LTCUSD",
    "BCHUSD", "DOTUSD", "LINKUSD", "AVAXUSD", "BNBUSD", "TONUSD",
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF",
    "GOLD", "SILVER", "OIL_CRUDE", "NATURALGAS",
    "US500", "US100", "US30", "DE40", "UK100", "JP225",
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL",
]


async def main(epics):
    load_dotenv()
    client = CapitalComClient(
        os.getenv("CAPITAL_API_KEY", ""),
        os.getenv("CAPITAL_IDENTIFIER", ""),
        os.getenv("CAPITAL_PASSWORD", ""),
    )
    if not await client.authenticate():
        print("auth failed")
        return 1
    rows = []
    for epic in epics:
        try:
            m = await client.get_market_details(epic)
        except Exception as e:
            rows.append((epic, "ERROR", str(e)[:60], "", "", ""))
            continue
        rows.append((
            epic,
            str(m.get("status") or "?"),
            f"{m.get('bid')}/{m.get('ask')}",
            f"min={m.get('min_size')}",
            f"step={m.get('size_step')}",
            f"max={m.get('max_size')}",
        ))
    width = max(len(r[0]) for r in rows) + 2
    for r in rows:
        print(r[0].ljust(width) + "  ".join(str(x).ljust(14) for x in r[1:]))
    await client.close()
    return 0


if __name__ == "__main__":
    args = sys.argv[1:] or CANDIDATES
    raise SystemExit(asyncio.run(main(args)))
