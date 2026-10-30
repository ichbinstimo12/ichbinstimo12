import asyncio
import logging
import json
from typing import Any, Dict, List, Callable, Awaitable
from src.firm.broker.client import CapitalComClient
from src.firm.bus.event_store import EventStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Reconciler")

class Reconciler:
    """
    Ensures internal state matches Broker truth.
    Calculates internal positions from the EventStore and compares them to the Broker API.
    """
    def __init__(self, broker: CapitalComClient, event_store: EventStore, interval: int = 10):
        self.broker = broker
        self.event_store = event_store
        self.interval = interval
        self.is_running = False
        self.consecutive_errors = 0

    def _calculate_internal_positions(self) -> Dict[str, float]:
        """
        Rebuild the firm's own book from the immutable event log.

        ORDER_FILLED adds exposure, POSITION_CLOSED removes it. Anything the
        broker closes on its own (stop-loss / take-profit) is reconciled by
        ``run()`` so the book never drifts.
        """
        positions: Dict[str, float] = {}
        events = self.event_store.get_events(types=["ORDER_FILLED", "POSITION_CLOSED"])

        for event in events:
            try:
                payload = json.loads(event["payload_json"])
            except (json.JSONDecodeError, TypeError):
                continue

            if event["type"] == "ORDER_FILLED":
                epic = payload.get("epic")
                side = payload.get("side")
                size = payload.get("size")
                if not epic or size is None:
                    continue
                change = float(size) if side == "BUY" else -float(size)
                positions[epic] = positions.get(epic, 0.0) + change

            elif event["type"] == "POSITION_CLOSED":
                epic = payload.get("epic")
                if not epic:
                    continue
                direction = str(payload.get("direction") or "").upper()
                size = float(payload.get("size") or positions.get(epic, 0.0) or 0.0)
                # Closing a long sells it, closing a short buys it back.
                if direction == "BUY":
                    positions[epic] = positions.get(epic, 0.0) - size
                elif direction == "SELL":
                    positions[epic] = positions.get(epic, 0.0) + size
                else:
                    positions[epic] = 0.0

        return {epic: round(size, 10) for epic, size in positions.items() if abs(size) > 1e-9}

    async def reconcile_once(self, on_mismatch: Callable[[], Awaitable[None]]) -> bool:
        """
        One reconciliation pass. Returns True when critical exposure was found.

        Only *unauthorised* exposure is critical (broker holds a position the
        firm never opened, or the sign flipped). A position the broker closed on
        its own is normal and is simply booked out.
        """
        broker_positions_list = await self.broker.get_positions()

        broker_positions: Dict[str, float] = {}
        for pos in broker_positions_list:
            epic = pos.get("epic")
            if not epic:
                continue
            volume = float(pos.get("volume") or 0.0)
            direction = str(pos.get("direction") or "BUY").upper()
            size = volume if direction == "BUY" else -volume
            broker_positions[epic] = broker_positions.get(epic, 0.0) + size

        internal_positions = self._calculate_internal_positions()

        all_epics = set(internal_positions) | set(broker_positions)
        critical = False

        for epic in all_epics:
            int_size = internal_positions.get(epic, 0.0)
            brk_size = broker_positions.get(epic, 0.0)

            if abs(int_size - brk_size) <= 1e-6:
                continue

            if abs(brk_size) <= 1e-9 and abs(int_size) > 0:
                # Broker closed it (stop-loss, take-profit, manual close).
                direction = "BUY" if int_size > 0 else "SELL"
                logger.info(f"{epic} closed at the broker (internal {int_size}); booking it out.")
                self.event_store.append(
                    "RECONCILER",
                    "POSITION_CLOSED",
                    {
                        "epic": epic,
                        "direction": direction,
                        "size": abs(int_size),
                        "reason": "closed at broker (stop-loss, take-profit or manual)",
                        "source": "reconciler",
                    },
                )
                continue

            if abs(int_size) <= 1e-9 and abs(brk_size) > 0:
                logger.critical(
                    f"UNAUTHORISED EXPOSURE in {epic}: broker holds {brk_size}, firm has no record."
                )
                critical = True
                continue

            if (int_size > 0) != (brk_size > 0):
                logger.critical(
                    f"STATE MISMATCH for {epic}: internal {int_size} vs broker {brk_size} (opposite sides)."
                )
                critical = True
                continue

            # Same side, different size (partial fill / partial close):
            # adopt broker truth instead of killing the firm.
            logger.warning(
                f"Size drift for {epic}: internal {int_size} vs broker {brk_size}. Adopting broker truth."
            )
            self.event_store.append(
                "RECONCILER",
                "POSITION_RECONCILED",
                {"epic": epic, "internal": int_size, "broker": brk_size},
            )

        if critical:
            await on_mismatch()
        return critical

    async def run(self, on_mismatch: Callable[[], Awaitable[None]]):
        """Loop to monitor account state."""
        self.is_running = True
        logger.info("Reconciler started: Monitoring internal vs broker state.")

        while self.is_running:
            try:
                await self.reconcile_once(on_mismatch)
                self._record_success()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._record_error(e)

            await asyncio.sleep(self.interval)

    ALARM_AFTER = 5     # consecutive failed comparisons -> RECONCILER_BLIND alarm (book cannot be verified)

    def _record_error(self, e: Exception) -> None:
        self.consecutive_errors += 1
        n = self.consecutive_errors
        logger.error(f"Reconciler loop error ({n} in a row): {e}")
        if n == self.ALARM_AFTER:
            self.event_store.append("RECONCILER", "RECONCILER_BLIND", {
                "consecutive_errors": n, "error": str(e)[:300],
                "note": "Positionen koennen nicht mit dem Broker abgeglichen werden - Broker/Internet pruefen"})

    def _record_success(self) -> None:
        if self.consecutive_errors >= self.ALARM_AFTER:
            self.event_store.append("RECONCILER", "RECONCILER_RECOVERED", {"after_errors": self.consecutive_errors})
            logger.info(f"Reconciler recovered after {self.consecutive_errors} failed comparisons.")
        self.consecutive_errors = 0

    def stop(self):
        self.is_running = False
