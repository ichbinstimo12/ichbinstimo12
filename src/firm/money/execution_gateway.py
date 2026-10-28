import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict
from pydantic import BaseModel, Field, field_validator
import uuid
from datetime import datetime
from src.firm.broker.client import CapitalComClient
from src.firm.money.risk_gate import RiskGate
from src.firm.bus.event_store import EventStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ExecutionGateway")

class TradeProposal(BaseModel):
    """Pydantic model for a trade request to ensure type safety."""
    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    epic: str
    side: str # "BUY" or "SELL"
    size: float
    stop_loss: float
    take_profit: Optional[float] = None
    reason: str
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    @field_validator('side')
    def validate_side(cls, v):
        if v not in ("BUY", "SELL"):
            raise ValueError("Side must be either BUY or SELL")
        return v

class ExecutionGateway:
    """
    Handles the lifecycle of an order from a validated Proposal to a Broker Fill.
    Enforces server-side Stop-Loss and persistent idempotency via EventStore.
    """
    def __init__(self, broker: CapitalComClient, risk_gate: RiskGate, event_store: Optional[EventStore] = None,
                 mode: str = "PAPER", live_token_path: str = "live_token.json"):
        self.broker = broker
        self.risk_gate = risk_gate
        self.event_store = event_store
        self.mode = mode
        self.live_token_path = live_token_path

    def _live_approved(self) -> bool:
        """Live modes require a valid, time-limited approval token (CLI: approve-live)."""
        if not str(self.mode).upper().startswith("LIVE"):
            return True
        path = Path(self.live_token_path)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return float(data.get("expires_at", 0)) > time.time()
        except Exception:
            return False

    async def execute(self, proposal: TradeProposal, account_balance: float, current_positions: int, current_price: Optional[float] = None) -> Dict[str, Any]:
        """
        The only path to place an order.
        Validates via RiskGate -> Checks Persistent Idempotency -> Places Order.
        """
        # 0. Mode ladder: no live orders without an explicit approval token
        if not self._live_approved():
            logger.critical(f"Live order blocked: approval token missing/expired for mode {self.mode}.")
            return {"status": "REJECTED", "reason": f"Live approval token missing or expired (mode {self.mode})"}

        # 1. Persistent Idempotency Check
        # We search the EventStore for any event where the payload contains this proposal_id
        if self.event_store is not None:
            events = self.event_store.get_events()
            for event in events:
                try:
                    import json
                    payload = json.loads(event['payload_json'])
                    if payload.get('proposal_id') == proposal.proposal_id:
                        logger.warning(f"Duplicate proposal {proposal.proposal_id} found in EventStore. Rejected.")
                        return {"status": "REJECTED", "reason": "Duplicate proposal"}
                except (json.JSONDecodeError, KeyError):
                    continue

        # 2. Risk Gate Validation
        allowed, reason = self.risk_gate.validate(
            type('obj', (object,), {
                'epic': proposal.epic,
                'side': proposal.side,
                'size': proposal.size,
                'stop_loss': proposal.stop_loss,
                'take_profit': proposal.take_profit,
                'reason': proposal.reason,
                'current_price': current_price,
            }),
            account_balance,
            current_positions,
            current_price
        )

        if not allowed:
            logger.info(f"RiskGate rejected proposal {proposal.proposal_id}: {reason}")
            return {"status": "REJECTED", "reason": reason}

        # 3. Execution
        try:
            logger.info(f"Executing {proposal.side} {proposal.size} {proposal.epic}...")

            # No more simulations. Real broker call.
            order_result = await self.broker.place_order(
                epic=proposal.epic,
                side=proposal.side,
                size=proposal.size,
                stop_loss=proposal.stop_loss,
                take_profit=proposal.take_profit
            )

            # Log the execution to EventStore for idempotency and audit
            self.event_store.append(
                agent_id="EXECUTION_GATEWAY",
                event_type="ORDER_FILLED",
                payload={
                    "proposal_id": proposal.proposal_id,
                    "order_id": order_result.get("dealId"),
                    "epic": proposal.epic,
                    "side": proposal.side,
                    "size": proposal.size,
                    "price": order_result.get("price"),
                    "timestamp": datetime.utcnow().isoformat()
                }
            )

            return {
                "status": "FILLED",
                "order_id": order_result.get("dealId"),
                "epic": proposal.epic,
                "filled_price": order_result.get("price"),
                "filled_size": order_result.get("size") or proposal.size,
            }

        except Exception as e:
            logger.error(f"Execution failed for {proposal.proposal_id}: {e}")
            return {"status": "ERROR", "reason": str(e)}
