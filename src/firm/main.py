import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from src.firm.bus.event_store import EventStore
from src.firm.broker.client import CapitalComClient
from src.firm.broker.mock_client import MockBrokerClient
from src.firm.money.risk_gate import RiskGate
from src.firm.money.execution_gateway import ExecutionGateway
from src.firm.money.kill_switch import KillSwitch
from src.firm.money.reconciler import Reconciler
from src.firm.money.trading_desk import TradingDesk
from src.firm.archive.store import Archive
from src.firm.archive.curator import ArchiveCurator
from src.firm.agents.runtime import AgentRuntime
from src.firm.agents.ceo_manager import CEOManager
from src.firm.agents.task_queue import TaskQueue, PENDING, RUNNING
from src.firm.agents.delegation import build_delegation_tasks
from src.firm.llm.factory import get_llm_provider

logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FirmOrchestrator")

HALT_FILE = Path(os.getenv("FIRM_HALT_FILE", "firm.halt"))
ROLES_DIR = Path(os.getenv("FIRM_ROLES_DIR", "config/roles"))
DEFAULT_ACTIVE_ROLES = "CEO,HeadOfResearch,QuantDeveloper,RiskOfficer,Archivist"


def env_secret(name: str) -> Optional[str]:
    """Read a secret from the environment, treating placeholders as missing."""
    value = os.getenv(name)
    if not value or value.strip() in ("", "HIER_EINFUEGEN"):
        return None
    return value


class FirmOrchestrator:
    """
    The main entry point that ties all modules together.
    Coordinates the Brain-Zone and Money-Zone.
    """
    def __init__(self):
        self.db_path = os.getenv("FIRM_DB_PATH", "firm_events.db")
        self.event_store = EventStore(self.db_path)
        self.task_queue = TaskQueue(self.db_path, self.event_store)
        self.is_running = False
        self.current_mode = "PAPER"

        # Load secrets from environment (placeholders count as missing)
        self.api_key = env_secret("CAPITAL_API_KEY")
        self.identifier = env_secret("CAPITAL_IDENTIFIER")
        self.password = env_secret("CAPITAL_PASSWORD")

        # LLM provider for the Brain-Zone (Ollama by default, Anthropic via upgrade)
        self.llm = get_llm_provider()

        # Initialize Components (will be assigned in start())
        self.broker = None
        self.risk_gate = None
        self.execution_gateway = None
        self.kill_switch = None
        self.reconciler = None
        self.archive = Archive(os.getenv("FIRM_ARCHIVE_DIR", "archiv"))
        self.curator = ArchiveCurator(
            self.archive,
            keep_tasks=int(os.getenv("FIRM_ARCHIVE_KEEP_TASKS", "40")),
            keep_trade_days=int(os.getenv("FIRM_ARCHIVE_KEEP_TRADE_DAYS", "3")),
        )
        self.agent_runtime = AgentRuntime(self.event_store, llm=self.llm, archive=self.archive)
        self.ceo = CEOManager(self.event_store)
        self._background_tasks = []

    async def start(self, mode: str = "PAPER"):
        self.current_mode = mode
        self.is_running = True

        logger.info(f"Initializing Firm in {mode} mode...")

        # 1. Broker Connection Attempt (only if credentials are really configured)
        broker_success = False
        if self.api_key and self.identifier and self.password:
            self.broker = CapitalComClient(self.api_key, self.identifier, self.password)
            try:
                if await self.broker.authenticate():
                    broker_success = True
                    logger.info("Connected to REAL Capital.com API.")
            except Exception as e:
                logger.warning(f"Real Broker connection failed: {e}")
        else:
            logger.warning("Capital.com credentials not configured (CAPITAL_API_KEY/IDENTIFIER/PASSWORD).")

        # 2. Fallback to Mock Broker if real one fails or is not configured
        if not broker_success:
            logger.warning("Switching to MOCK BROKER for development/simulation...")
            self.broker = MockBrokerClient(self.api_key or "MOCK_KEY", self.identifier or "MOCK_ID", self.password or "MOCK_PASS")
            await self.broker.authenticate()
            logger.info("Connected to Mock Broker.")

        # 3. Initialize Money-Zone with the chosen broker
        self.risk_gate = RiskGate("config/limits.toml", "config/whitelist.toml")
        self.execution_gateway = ExecutionGateway(self.broker, self.risk_gate, self.event_store, mode=mode)
        self.kill_switch = KillSwitch(self.broker)
        self.reconciler = Reconciler(self.broker, self.event_store)

        # 3b. Deterministic trading desk. The LLM never sizes or places a trade.
        self.trading_desk = None
        if os.getenv("FIRM_TRADING", "1").strip().lower() not in ("0", "false", "no", "off"):
            try:
                self.trading_desk = TradingDesk(
                    broker=self.broker,
                    gateway=self.execution_gateway,
                    risk_gate=self.risk_gate,
                    event_store=self.event_store,
                    mode=mode,
                    resolution=os.getenv("FIRM_TRADE_RESOLUTION", "HOUR"),
                    cooldown_seconds=float(os.getenv("FIRM_TRADE_COOLDOWN", "0")),
                    archive=self.archive,
                )
                logger.info(
                    f"Trading desk armed: {', '.join(self.trading_desk.epics)} "
                    f"({self.trading_desk.resolution})"
                )
            except Exception as e:
                logger.error(f"Trading desk could not start: {e}")

        # 4. Load role configs (Brain-Zone staffing)
        active_roles = [n.strip() for n in os.getenv("FIRM_ACTIVE_ROLES", DEFAULT_ACTIVE_ROLES).split(",") if n.strip()]
        loaded = self.agent_runtime.load_agents_from_dir(str(ROLES_DIR), only=active_roles)
        if loaded:
            logger.info(f"Active agents: {', '.join(loaded)}")
        else:
            logger.warning("No agents loaded — firm will idle. Check config/roles/*.yaml")

        # 5. Crash recovery: re-queue tasks that a previous run left unfinished
        self.task_queue.requeue_stale()

        # 5b. Import trades that happened before the archive existed (idempotent)
        try:
            await asyncio.to_thread(self.archive.backfill_from_events, self.event_store)
        except Exception as e:
            logger.warning(f"Archive backfill skipped: {e}")

        # 6. Background loops: reconciliation, CEO delegation, agent workers, account snapshots
        self._background_tasks = [
            asyncio.create_task(self.reconciler.run(on_mismatch=self.handle_mismatch)),
            asyncio.create_task(self.delegation_loop()),
            asyncio.create_task(self.agent_loop()),
            asyncio.create_task(self.account_snapshot_loop()),
            asyncio.create_task(self.archive_loop()),
        ]
        if self.trading_desk is not None:
            self._background_tasks.append(asyncio.create_task(self.trading_loop()))

        logger.info(f"Firm is now LIVE (Mode: {self.current_mode}). Monitoring markets...")

        try:
            while self.is_running:
                # Operator halt via CLI: `python -m src.firm halt`
                if HALT_FILE.exists():
                    logger.critical(f"HALT flag detected ({HALT_FILE}). Activating kill switch and stopping.")
                    if self.kill_switch:
                        await self.kill_switch.activate()
                    self.is_running = False
                    break
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def delegation_loop(self):
        """
        CEO delegation loop: whenever the queue is empty, the CEO assigns the
        standing agenda to every active agent.
        """
        interval = float(os.getenv("FIRM_DELEGATION_SECONDS", "60"))
        while self.is_running:
            try:
                counts = self.task_queue.count_by_status()
                if counts[PENDING] == 0 and counts[RUNNING] == 0:
                    plan = build_delegation_tasks(list(self.agent_runtime.active_agents.keys()))
                    for item in plan:
                        self.task_queue.create_task(
                            created_by="CEO",
                            assigned_to=item["assigned_to"],
                            title=item["title"],
                            description=item["description"],
                        )
                    if plan:
                        logger.info(f"CEO delegated {len(plan)} task(s).")
            except Exception as e:
                logger.error(f"Delegation loop error: {e}")
            await asyncio.sleep(interval)

    def _build_observation(self, agent_name: str, task: dict) -> str:
        """Build a real observation from the immutable event store."""
        events = self.event_store.get_events()[-5:]
        context = "; ".join(
            f"{e.get('type')}@{e.get('agent_id')}: {e.get('payload_json')}" for e in events
        )
        text = (
            f"Task: {task['title']} - {task['description']}\n"
            f"Recent firm events: {context or 'none'}"
        )
        if agent_name == "Archivist":
            text += "\n" + self._archive_observation()
        return text

    def _archive_observation(self) -> str:
        """Real archive numbers for the cleaning agent (never estimated)."""
        try:
            stats = self.archive.stats()
            trades = self.archive.trades(limit=8)
        except Exception as e:
            return f"Archive stats unavailable: {e}"

        lines = [
            f"Archive: {stats['trades_total']} fills, {stats['trades_closed']} closed, "
            f"realized {stats['realized_profit']:+.2f}, "
            f"{stats['compressed_files']} compressed file(s), "
            f"{stats['size_bytes'] / 1024:.1f} KiB on disk."
        ]
        for t in trades:
            pnl = t.get("profit")
            pnl_txt = f"{pnl:+.2f}" if isinstance(pnl, (int, float)) else "open"
            lines.append(
                f"- {str(t.get('time') or '')[:16]} {t.get('side')} {t.get('size')} "
                f"{t.get('epic')} @ {t.get('entry')} -> {t.get('exit') or 'open'} ({pnl_txt})"
            )
        return "\n".join(lines)

    async def agent_loop(self):
        """
        Worker loop: every active agent claims its next pending task and runs
        the Observe -> Think -> Act cycle through the LLM provider.
        """
        interval = float(os.getenv("FIRM_TICK_SECONDS", "15"))
        logger.info("Agent loop started.")
        while self.is_running:
            for agent_name, config in list(self.agent_runtime.active_agents.items()):
                if not self.is_running:
                    break
                if config is None:
                    continue

                try:
                    task = self.task_queue.next_pending(agent_name)
                except Exception as e:
                    logger.error(f"Task claim failed for {agent_name}: {e}")
                    continue
                if not task:
                    continue

                try:
                    observation = self._build_observation(agent_name, task)
                    result = await self.agent_runtime.step(agent_name, observation, task["task_id"])
                    self.task_queue.complete(task["task_id"], result)
                    self._archive_task(agent_name, task, result=result)
                    logger.info(f"Completed task {task['task_id']} for {agent_name}")
                except Exception as e:
                    logger.error(f"Agent {agent_name} failed task {task['task_id']}: {e}")
                    self.task_queue.fail(task["task_id"], str(e))
                    self._archive_task(agent_name, task, error=str(e))

            await asyncio.sleep(interval)

    def _archive_task(self, agent_name: str, task: dict, result=None, error: Optional[str] = None) -> None:
        """File a completed (or failed) task with the actions it produced."""
        try:
            actions = self.event_store.get_events(correlation_id=task.get("task_id"))
        except Exception:
            actions = []
        try:
            self.archive.record_task(agent_name, task, result=result, actions=actions, error=error)
        except Exception as e:
            logger.warning(f"Could not archive task {task.get('task_id')}: {e}")

    async def archive_loop(self):
        """
        The cleaning agent's deterministic half: keep the latest task files,
        compress everything older, never delete a trade.

        Runs on its own timer so a busy LLM cannot starve it, and so a long
        market session does not fill the folder with thousands of small files.
        """
        interval = float(os.getenv("FIRM_ARCHIVE_SECONDS", "3600"))
        logger.info(f"Archive loop started (every {interval:.0f}s).")
        while self.is_running:
            try:
                report = await asyncio.to_thread(self.curator.run)
                if report.get("tasks_compressed") or report.get("trade_events_compressed"):
                    logger.info(
                        f"Archive cleaned: {report['tasks_compressed']} task file(s) and "
                        f"{report['trade_events_compressed']} trade event(s) compressed."
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Archive loop error: {e}")
            await asyncio.sleep(interval)

    async def trading_loop(self):
        """
        Deterministic market scan. Runs independently of the LLM so the firm
        keeps trading even while the agents are thinking (or the model is down).
        """
        interval = float(os.getenv("FIRM_TRADE_SECONDS", "180"))
        timeout = float(os.getenv("FIRM_TRADE_TIMEOUT", "120"))
        logger.info(f"Trading loop started (every {interval:.0f}s).")
        cycle = 0
        while self.is_running:
            cycle += 1
            try:
                summary = await asyncio.wait_for(self.trading_desk.run_cycle(), timeout=timeout)
                if summary.get("trades"):
                    logger.info(f"Trading desk executed {len(summary['trades'])} trade(s).")
                elif summary.get("closed"):
                    logger.info(f"Trading desk closed {len(summary['closed'])} position(s).")
                else:
                    reasons = "; ".join(
                        f"{s.get('epic')}: {s.get('reason')}" for s in summary.get("skipped", [])
                    ) or "no markets scanned"
                    logger.info(f"Desk cycle {cycle}: no entry - {reasons}")
            except asyncio.TimeoutError:
                logger.error(f"Desk cycle {cycle} timed out after {timeout:.0f}s; retrying next interval.")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Trading loop error: {e}")
            await asyncio.sleep(interval)

    async def account_snapshot_loop(self):
        """Publish real account snapshots (with open positions) for the dashboard."""
        interval = float(os.getenv("FIRM_ACCOUNT_SECONDS", "60"))
        while self.is_running:
            try:
                details = await self.broker.get_account_details()
                if isinstance(details, dict):
                    snapshot = dict(details)
                    snapshot["source"] = "mock" if isinstance(self.broker, MockBrokerClient) else "broker"
                    try:
                        positions = await self.broker.get_positions()
                        snapshot["positions"] = positions
                        snapshot["open_positions"] = len(positions)
                    except Exception as e:
                        logger.warning(f"Position snapshot failed: {e}")
                    self.event_store.append("SYSTEM", "ACCOUNT_SNAPSHOT", snapshot)
            except Exception as e:
                logger.warning(f"Account snapshot failed: {e}")
            await asyncio.sleep(interval)

    async def stop(self):
        if getattr(self, "_stopped", False):
            return
        self._stopped = True
        logger.info("Stopping Firm Orchestrator...")
        self.is_running = False
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks = []
        if self.agent_runtime is not None:
            await self.agent_runtime.close()
        if self.reconciler:
            self.reconciler.stop()
        if self.broker:
            await self.broker.close()

    async def handle_mismatch(self):
        logger.critical("RECONCILER MISMATCH DETECTED! Triggering KillSwitch...")
        if self.kill_switch:
            await self.kill_switch.activate()
        self.is_running = False
