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
from src.firm.agents.task_queue import TaskQueue
from src.firm.agents.delegation import AgentScheduler
from src.firm.agents.facts import build_facts
from src.firm.money.risk_overlay import RiskOverlay
from src.firm.money.strategy_gate import StrategyGate
from src.firm.research import pipeline
from src.firm.tools.capital_mcp import CapitalMCPTools
from src.firm.llm.factory import get_llm_provider

logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FirmOrchestrator")

HALT_FILE = Path(os.getenv("FIRM_HALT_FILE", "firm.halt"))
ROLES_DIR = Path(os.getenv("FIRM_ROLES_DIR", "config/roles"))
DEFAULT_ACTIVE_ROLES = "CEO,HeadOfResearch,RiskOfficer,MacroAnalyst,ExecutionTrader,RedTeam"


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
        self.gate = StrategyGate()
        self.overlay = RiskOverlay(self.db_path, self.event_store)
        self.tools = CapitalMCPTools()
        import tomllib
        with open("config/whitelist.toml", "rb") as f:
            self.whitelist = list(tomllib.load(f).get("allowed_epics") or [])
        with open("config/limits.toml", "rb") as f:
            self.limits = tomllib.load(f)
        self.agent_runtime = AgentRuntime(self.event_store, llm=self.llm, archive=self.archive,
                                          tools=self.tools, whitelist=self.whitelist,
                                          firm_token_budget=int(self.limits.get("operational", {})
                                                                .get("max_token_budget_daily", 0)) or None)
        self.ceo = CEOManager(self.event_store, on_approved=self.hire_role)
        self.scheduler = None
        self._background_tasks = []
        self._research_running = False
        self._research_requested: Optional[str] = None
        self._last_research_attempt = 0.0

    async def start(self, mode: str = "PAPER"):
        self.current_mode = mode
        self.is_running = True

        logger.info(f"Initializing Firm in {mode} mode...")

        # 1. Broker connection. With credentials we NEVER fall back to the mock silently: a failed login used to
        #    leave the firm running on a simulation that cannot trade, without anyone noticing. The real client
        #    logs in again by itself on the next request, so the desk recovers once the broker is reachable.
        if self.api_key and self.identifier and self.password:
            self.broker = CapitalComClient(self.api_key, self.identifier, self.password)
            for attempt in range(1, 4):
                try:
                    await self.broker.authenticate()
                    logger.info("Connected to REAL Capital.com API.")
                    break
                except Exception as e:
                    logger.error(f"Broker login failed (attempt {attempt}/3): {e}")
                    if attempt == 3:
                        self.event_store.append("SYSTEM", "BROKER_OFFLINE", {"error": str(e)[:300]})
                    else:
                        await asyncio.sleep(5 * attempt)
        else:
            logger.warning("Capital.com credentials not configured (CAPITAL_API_KEY/IDENTIFIER/PASSWORD). "
                           "Using MOCK broker: no trading, no research.")
            self.broker = MockBrokerClient("MOCK_KEY", "MOCK_ID", "MOCK_PASS")
            await self.broker.authenticate()

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
                    gate=self.gate,
                    overlay=self.overlay,
                    halt_file=str(HALT_FILE),
                    max_api_errors=int(os.getenv("FIRM_MAX_API_ERRORS", "10")),
                    api_error_window_s=float(os.getenv("FIRM_API_ERROR_WINDOW_MIN", "30")) * 60,
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

        # 5a. Read-only Capital.com tools for the agents (MCP, demo credentials, trading disabled server-side)
        if not isinstance(self.broker, MockBrokerClient):
            await self.tools.start()
        logger.info(f"Agent tools: {'MCP ' + ', '.join(self.tools.tools) if self.tools.available else self.tools.reason}")
        self.scheduler = AgentScheduler(self.event_store, self.task_queue, self.agent_runtime.active_agents)

        # 5b. Import trades that happened before the archive existed (idempotent)
        try:
            await asyncio.to_thread(self.archive.backfill_from_events, self.event_store)
        except Exception as e:
            logger.warning(f"Archive backfill skipped: {e}")

        # 6. Background loops: reconciliation, CEO delegation, agent workers, account snapshots
        self._background_tasks = [
            asyncio.create_task(self.reconciler.run(on_mismatch=self.handle_mismatch)),
            asyncio.create_task(self.scheduler_loop()),
            asyncio.create_task(self.research_loop()),
            asyncio.create_task(self.report_loop()),
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

    async def scheduler_loop(self):
        """Event-driven task creation (see agents/delegation.py). Replaces the old 60-second standing agenda."""
        interval = float(os.getenv("FIRM_SCHEDULER_SECONDS", "30"))
        while self.is_running:
            try:
                created = self.scheduler.tick()
                if created:
                    logger.info(f"Scheduler created {len(created)} task(s).")
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            await asyncio.sleep(interval)

    # ---------------------------------------------------------------- research
    def research_due(self) -> Optional[str]:
        """Why a research run should start now (None = not due)."""
        max_age_days = float(os.getenv("FIRM_RESEARCH_DAYS", "7"))
        if self._research_requested:
            return f"requested: {self._research_requested}"
        if self.gate.is_stale(max_age_days * 86400):
            return "no verdicts yet" if not self.gate.created() else f"verdicts older than {max_age_days:g} days"
        return None

    async def run_research(self, why: str) -> None:
        if self._research_running or self.trading_desk is None:
            return
        if isinstance(self.broker, MockBrokerClient):
            logger.warning("Research skipped: no real broker connection (mock).")
            return
        self._research_running = True
        self._last_research_attempt = asyncio.get_running_loop().time()
        resolution = self.trading_desk.resolution
        days = int(os.getenv("FIRM_RESEARCH_HISTORY_DAYS", "365"))
        self.event_store.append("RESEARCH_LAB", "RESEARCH_STARTED", {"why": why, "epics": self.whitelist,
                                                                     "resolution": resolution, "days": days})
        logger.info(f"Research run started ({why}): {', '.join(self.whitelist)} {resolution} {days}d")
        try:
            data = await pipeline.fetch_history(self.broker, self.whitelist, resolution, days, log=logger.info)
            markets, account = await self._sizing_inputs()
            verdicts, lines, trials = await asyncio.to_thread(
                pipeline.run_research, data, resolution, pipeline.previous_trials(self.gate.path), logger.info,
                markets, account)
            await asyncio.to_thread(pipeline.write_outputs, verdicts, lines, trials, resolution, self.gate.path.parent)
            self.event_store.append("RESEARCH_LAB", "RESEARCH_DONE", {"trials": trials, "resolution": resolution,
                                                                      "summary": pipeline.summarize(verdicts)})
            logger.info(f"Research run done: {pipeline.summarize(verdicts)}")
            self._research_requested = None
        except Exception as e:
            logger.error(f"Research run failed: {e}")
            self.event_store.append("RESEARCH_LAB", "RESEARCH_FAILED", {"error": str(e)[:500], "why": why})
        finally:
            self._research_running = False

    async def _sizing_inputs(self):
        """Real broker numbers for the minimum-size feasibility check (missing values -> check skipped)."""
        markets, account = {}, None
        try:
            acct = await self.broker.get_account_details()
            account = {"balance": float(acct.get("balance") or 0),
                       "risk_pct": float(self.trading_desk.limits["trade"]["max_risk_per_trade"])}
            for epic in self.whitelist:
                try:
                    m = await self.broker.get_market_details(epic)
                    bid, ask = m.get("bid"), m.get("ask")
                    fx = await self.trading_desk._fx(m.get("currency"), acct.get("currency"))
                    if bid and ask and fx:
                        markets[epic] = {"price": (float(bid) + float(ask)) / 2, "min_size": m.get("min_size"), "fx": fx}
                except Exception as e:
                    logger.warning(f"Sizing inputs for {epic} unavailable: {e}")
        except Exception as e:
            logger.warning(f"Account unavailable for sizing check: {e}")
        return markets, account

    async def report_loop(self):
        """Daily report for the previous UTC day (Phase 5 of the spec), written once per day."""
        from datetime import datetime, timedelta, timezone
        from src.firm.reports.daily import DailyReporter
        reporter = DailyReporter(self.event_store, os.getenv("FIRM_REPORT_DIR", "reports"))
        while self.is_running:
            try:
                day = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
                if not (reporter.out_dir / f"daily_{day}.md").exists():
                    report = await asyncio.to_thread(reporter.generate_report, day)
                    path = await asyncio.to_thread(reporter.save_to_disk, report)
                    self.event_store.append("SYSTEM", "DAILY_REPORT", {"date": day, "path": str(path),
                                                                       "fills": report["fills"],
                                                                       "closed_trades": report["closed_trades"]})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Daily report failed: {e}")
            await asyncio.sleep(1800)

    async def research_loop(self):
        """Keeps the strategy verdicts fresh: at start when missing, then weekly or on request (min. 1 h apart)."""
        await asyncio.sleep(float(os.getenv("FIRM_RESEARCH_START_DELAY", "20")))
        while self.is_running:
            try:
                why = self.research_due()
                loop_time = asyncio.get_running_loop().time()
                if why and (not self._last_research_attempt or loop_time - self._last_research_attempt > 3600):
                    await self.run_research(why)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Research loop error: {e}")
            await asyncio.sleep(300)

    # ----------------------------------------------------------------- agents
    def _role_templates(self) -> dict:
        """name -> path of every role template in config/roles."""
        import yaml
        out = {}
        for path in sorted(ROLES_DIR.glob("*.yaml")):
            try:
                out[yaml.safe_load(path.read_text(encoding="utf-8"))["name"]] = path
            except Exception:
                continue
        return out

    def hire_role(self, role: str) -> None:
        """Called after the OPERATOR approved a hiring request. Loads the role template into the running firm."""
        max_agents = int(self.limits.get("operational", {}).get("max_agents", 10))
        if len(self.agent_runtime.active_agents) >= max_agents:
            self.event_store.append("SYSTEM", "HIRING_BLOCKED", {"role": role, "reason": f"max_agents {max_agents}"})
            return
        path = self._role_templates().get(role)
        if path is None:
            self.event_store.append("SYSTEM", "HIRING_BLOCKED", {"role": role, "reason": "no such role template"})
            return
        self.agent_runtime.load_agent(str(path))
        self.event_store.append("SYSTEM", "AGENT_HIRED", {"role": role})
        logger.info(f"Agent {role} hired after operator approval.")

    def _staff_facts(self) -> dict:
        from src.firm.agents.metrics import agent_metrics, tokens_today
        active = list(self.agent_runtime.active_agents)
        pending = {}
        for t in self.task_queue.list_tasks(status="PENDING", limit=500):
            pending[t["assigned_to"]] = pending.get(t["assigned_to"], 0) + 1
        metrics = agent_metrics(self.event_store, days=7)
        return {
            "active": active,
            "available_roles": [r for r in self._role_templates() if r not in active],
            "pending_tasks": pending,
            "tokens_today": tokens_today(self.event_store),
            "valid_rate_7d": {a: (metrics.get(a) or {}).get("valid_rate") for a in active},
        }

    def _facts(self, agent_name: str = "") -> dict:
        facts = build_facts(self.event_store, self.current_mode, gate=self.gate, overlay=self.overlay,
                            desk=self.trading_desk)
        if agent_name == "CEO":
            facts["staff"] = self._staff_facts()
        return facts

    def _build_observation(self, agent_name: str, task: dict) -> str:
        text = f"{task['title']}\n{task['description']}"
        if agent_name == "Archivist":
            text += "\n" + self._archive_observation()
        return text

    async def handle_agent_action(self, agent: str, action: dict) -> dict:
        """Execute a validated agent action. Only risk-reducing or work-requesting actions exist."""
        kind = action["type"]
        if kind in ("tighten_risk", "block_entries"):
            row = self.overlay.tighten(action["epic"], action["factor"], action["hours"], agent, action["reason"],
                                       source=action.get("source"))
            return {"status": "applied", "overlay_id": row["id"], "factor": row["factor"],
                    "until": row["expires_at"]}
        if kind == "request_research":
            if self._research_running:
                return {"status": "already running"}
            if not self.gate.is_stale(24 * 3600):
                return {"status": "declined", "reason": "verdicts are younger than 24 h"}
            self._research_requested = f"{agent}: {action['reason']}"
            return {"status": "queued"}
        if kind == "flag":
            self.event_store.append(agent, "FLAG", {"severity": action["severity"], "message": action["message"]})
            return {"status": "posted"}
        if kind == "propose_hire":
            role = action["role"]
            if role in self.agent_runtime.active_agents:
                return {"status": "declined", "reason": f"{role} is already active"}
            if role not in self._role_templates():
                return {"status": "declined", "reason": f"no role template named {role}"}
            if any(r.role == role and r.status.value == "PENDING" for r in self.ceo.hiring_requests.values()):
                return {"status": "declined", "reason": "request already pending"}
            import yaml
            cfg = yaml.safe_load(self._role_templates()[role].read_text(encoding="utf-8"))
            req = self.ceo.create_hiring_request(role, f"{agent}: {action['reason']}",
                                                 {"tool_allowlist": cfg.get("tool_allowlist"),
                                                  "actions": cfg.get("actions")},
                                                 int(cfg.get("daily_token_budget") or 0))
            return {"status": "awaiting operator approval", "request_id": req}
        return {"status": "ignored"}

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
                    result = await self.agent_runtime.step(agent_name, observation, task["task_id"],
                                                           facts=self._facts(agent_name), on_action=self.handle_agent_action)
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
        idle_hours = float(os.getenv("FIRM_IDLE_ALERT_HOURS", "12"))
        logger.info(f"Trading loop started (every {interval:.0f}s).")
        cycle = 0
        loop = asyncio.get_running_loop()
        last_activity = loop.time()
        while self.is_running:
            cycle += 1
            # Idle watchdog: say loudly (event -> CEO -> dashboard) why nothing has traded for hours.
            fills = self.event_store.get_events(types=["ORDER_FILLED"], last=1)
            if fills and fills[-1]["ts"] != getattr(self, "_last_fill_seen", None):
                self._last_fill_seen = fills[-1]["ts"]
                last_activity = loop.time()
            if loop.time() - last_activity > idle_hours * 3600:
                self.event_store.append("TRADING_DESK", "DESK_IDLE", {
                    "hours_without_entry": idle_hours,
                    "why_no_entry": (self.trading_desk.last_summary or {}).get("reasons"),
                    "error": (self.trading_desk.last_summary or {}).get("error"),
                    "research": self.gate.overview().get("verdicts"),
                })
                last_activity = loop.time()
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
        await self.tools.close()
        if self.reconciler:
            self.reconciler.stop()
        if self.broker:
            await self.broker.close()

    async def handle_mismatch(self):
        logger.critical("RECONCILER MISMATCH DETECTED! Triggering KillSwitch...")
        if self.kill_switch:
            await self.kill_switch.activate()
        self.is_running = False
