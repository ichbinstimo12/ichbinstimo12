import asyncio
import logging
import os
import time
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
from src.firm.paths import data_dir

logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FirmOrchestrator")

from src.firm.research.lab import RES_SECONDS  # noqa: E402

HALT_FILE = Path(os.getenv("FIRM_HALT_FILE", "firm.halt"))
ROLES_DIR = Path(os.getenv("FIRM_ROLES_DIR", "config/roles"))
DEFAULT_ACTIVE_ROLES = "CEO,HeadOfResearch,RiskOfficer,MacroAnalyst,ExecutionTrader,RedTeam"


def research_resolutions(desk_default: str) -> list:
    """Zeitrahmen der Forschung. Standard (FIRM_STYLE=daytrade): nur Intraday, 15 und 5 Minuten; je Markt gewinnt,
    was die Pruefung besteht. Swing-Zeitrahmen aus einer alten .env werden im Daytrading-Modus ignoriert."""
    from src.firm.money.strategy_gate import INTRADAY, trading_style
    raw = os.getenv("FIRM_RESEARCH_RESOLUTIONS", "MINUTE_15,MINUTE_5")
    out = [r.strip().upper() for r in raw.split(",") if r.strip()]
    if trading_style() == "daytrade":
        out = [r for r in out if r in INTRADAY] or ["MINUTE_15", "MINUTE_5"]
    return out or [desk_default]


def history_days(resolution: str) -> int:
    """Historienlaenge je Zeitrahmen (ANNAHME: Capital.com liefert so weit zurueck; der Bericht zeigt die echte Spanne)."""
    intraday = {"MINUTE": 30, "MINUTE_5": 60, "MINUTE_15": 120, "MINUTE_30": 180}
    default = intraday.get(resolution) or int(os.getenv("FIRM_RESEARCH_HISTORY_DAYS", "365"))
    return int(os.getenv(f"FIRM_RESEARCH_HISTORY_DAYS_{resolution}", default))


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
        from src.firm.agents.playbooks import PlaybookStore
        self.playbooks = PlaybookStore(self.db_path, event_store=self.event_store)
        self.agent_runtime = AgentRuntime(self.event_store, llm=self.llm, archive=self.archive,
                                          tools=self.tools, whitelist=self.whitelist, playbooks=self.playbooks,
                                          firm_token_budget=int(self.limits.get("operational", {})
                                                                .get("max_token_budget_daily", 0)) or None)
        self.ceo = CEOManager(self.event_store, on_approved=self.hire_role)
        self.scheduler = None
        self.trading_desk = None
        self.stream = None
        self.scanner = None
        self._background_tasks = []
        self._research_running = False
        self.zones = {"money", "brain"}
        self._last_research_attempt = 0.0

    async def start(self, mode: str = "PAPER", zones=("money", "brain")):
        """
        zones: "money" = broker, desk, risk, reconciler, kill switch, research, reports (the only zone with broker keys)
               "brain" = agents, scheduler, LLM, read-only MCP tools (no broker keys)
        The supervisor (`python -m src.firm run`) starts each zone as its own process; `--single` runs both here.
        """
        self.current_mode = mode
        self.is_running = True
        self.zones = set(zones)
        logger.info(f"Initializing Firm in {mode} mode (zones: {', '.join(sorted(self.zones))})...")
        if "money" in self.zones:
            await self._start_money(mode)
        if "brain" in self.zones:
            await self._start_brain()
        await self._run_until_halt()

    async def _start_money(self, mode: str):
        from src.firm.money.limits_check import limit_warnings
        for w in limit_warnings(self.limits):
            logger.warning(f"LIMITS: {w}")
            self.event_store.append("RISK", "LIMITS_WARNING", {"warning": w, "file": "config/limits.toml"})

        # 1. Broker connection. With credentials we NEVER fall back to the mock silently: a failed login used to
        #    leave the firm running on a simulation that cannot trade, without anyone noticing. The real client
        #    logs in again by itself on the next request, so the desk recovers once the broker is reachable.
        if str(mode).upper().startswith("LIVE"):
            # Spec: separate keys for live. Demo keys never trade live, live keys never run in PAPER.
            self.api_key = env_secret("CAPITAL_LIVE_API_KEY")
            self.identifier = env_secret("CAPITAL_LIVE_IDENTIFIER")
            self.password = env_secret("CAPITAL_LIVE_PASSWORD")
            if not (self.api_key and self.identifier and self.password):
                raise RuntimeError("LIVE mode needs CAPITAL_LIVE_API_KEY / _IDENTIFIER / _PASSWORD in .env")
        if self.api_key and self.identifier and self.password:
            live = str(mode).upper().startswith("LIVE")
            from src.firm.broker.client import DEMO_BASE_URL, LIVE_BASE_URL
            base = (os.getenv("CAPITAL_BASE_URL_LIVE") or LIVE_BASE_URL) if live else \
                (os.getenv("CAPITAL_BASE_URL_DEMO") or DEMO_BASE_URL)
            self.broker = CapitalComClient(self.api_key, self.identifier, self.password, base_url=base)
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
        self.execution_gateway = ExecutionGateway(self.broker, self.risk_gate, self.event_store, mode=mode,
                                                  live_token_path=os.getenv("FIRM_LIVE_TOKEN_FILE", "live_token.json"))
        self.kill_switch = KillSwitch(self.broker)
        self.reconciler = Reconciler(self.broker, self.event_store)

        # 3a. Live prices (WebSocket). Read-only; the desk falls back to REST quotes while it is down.
        self.stream = None
        if (not isinstance(self.broker, MockBrokerClient) and hasattr(self.broker, "stream_credentials")
                and os.getenv("FIRM_STREAM", "1").strip().lower() not in ("0", "false", "no", "off")):
            from src.firm.broker.stream import PriceStream
            self.stream = PriceStream(self.broker.stream_credentials, self.whitelist, event_store=self.event_store)

        # 3a. Market scanner: whole Capital.com list -> best candidates into the stream, 1-second scan.
        self.scanner = None
        if (not isinstance(self.broker, MockBrokerClient)
                and os.getenv("FIRM_SCANNER", "1").strip().lower() not in ("0", "false", "no", "off")):
            from src.firm.money.scanner import MarketScanner
            self.scanner = MarketScanner(
                self.broker, self.stream, self.event_store, self.whitelist,
                resolution=os.getenv("FIRM_SCANNER_RESOLUTION", "MINUTE_5"),
                universe_size=int(os.getenv("FIRM_SCANNER_UNIVERSE", "36")),
                universe_hours=float(os.getenv("FIRM_SCANNER_UNIVERSE_HOURS", "6")),
                rps=float(os.getenv("FIRM_SCANNER_RPS", "2")),
                max_spread_pct=float(os.getenv("FIRM_SCANNER_MAX_SPREAD_PCT", "0.1")))

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
                    state_path=os.getenv("FIRM_STATE_PATH", "firm_state.json"),
                    resolution=os.getenv("FIRM_TRADE_RESOLUTION", "MINUTE_15"),
                    cooldown_seconds=float(os.getenv("FIRM_TRADE_COOLDOWN", "0")),
                    archive=self.archive,
                    gate=self.gate,
                    overlay=self.overlay,
                    halt_file=str(HALT_FILE),
                    stream=self.stream,
                    quote_max_age=float(os.getenv("FIRM_QUOTE_MAX_AGE", "15")),
                    fast_loss_pct=float(os.getenv("FIRM_FAST_LOSS_PCT", "1.0")) / 100,
                    fast_loss_window_s=float(os.getenv("FIRM_FAST_LOSS_MINUTES", "60")) * 60,
                    max_avg_slippage_r=float(os.getenv("FIRM_MAX_AVG_SLIPPAGE_R", "0.3")),
                    max_api_errors=int(os.getenv("FIRM_MAX_API_ERRORS", "10")),
                    api_error_window_s=float(os.getenv("FIRM_API_ERROR_WINDOW_MIN", "30")) * 60,
                )
                logger.info(
                    f"Trading desk armed: {', '.join(self.trading_desk.epics)} "
                    f"({self.trading_desk.resolution})"
                )
            except Exception as e:
                logger.error(f"Trading desk could not start: {e}")

        # 5b. Import trades that happened before the archive existed (idempotent)
        try:
            await asyncio.to_thread(self.archive.backfill_from_events, self.event_store)
        except Exception as e:
            logger.warning(f"Archive backfill skipped: {e}")

        sup = self._supervised
        self._background_tasks += [
            sup("reconciler", lambda: self.reconciler.run(on_mismatch=self.handle_mismatch)),
            sup("research_loop", self.research_loop),
            sup("report_loop", self.report_loop),
            sup("account_snapshot_loop", self.account_snapshot_loop),
            sup("archive_loop", self.archive_loop),
        ]
        if self.trading_desk is not None:
            self._background_tasks.append(sup("trading_loop", self.trading_loop))
        if self.stream is not None:
            self._background_tasks.append(sup("price_stream", self.stream.run))
        if self.scanner is not None:
            self._background_tasks.append(sup("scanner", lambda: self.scanner.run(float(os.getenv("FIRM_SCAN_SECONDS", "1")))))

    async def _start_brain(self):
        # 4. Load role configs (Brain-Zone staffing) + agents hired earlier with operator approval
        active_roles = [n.strip() for n in os.getenv("FIRM_ACTIVE_ROLES", DEFAULT_ACTIVE_ROLES).split(",") if n.strip()]
        # Roles added after an .env was written (FIRM_ACTIVE_ROLES there predates them); "-" disables.
        added = os.getenv("FIRM_ADD_ROLES", "SMCAnalyst,SMCTrader,Archivist")
        if added.strip() != "-":
            active_roles += [n.strip() for n in added.split(",") if n.strip() and n.strip() not in active_roles]
        loaded = self.agent_runtime.load_agents_from_dir(str(ROLES_DIR), only=active_roles)
        if loaded:
            logger.info(f"Active agents: {', '.join(loaded)}")
        else:
            logger.warning("No agents loaded — firm will idle. Check config/roles/*.yaml")

        # 5. Crash recovery: re-queue tasks that a previous run left unfinished
        self.task_queue.requeue_stale()

        for req in self.ceo.approved_unapplied():
            if req.role not in self.agent_runtime.active_agents:
                self.hire_role(req.role)
            self.ceo.mark_applied(req.request_id)
        self._sync_registry()

        # 5a. Read-only Capital.com tools for the agents (MCP, demo credentials, trading disabled server-side).
        #     Split mode: the supervisor runs the server and passes FIRM_MCP_URL; this process has no broker keys.
        if os.getenv("FIRM_MCP_URL") or not isinstance(self.broker, MockBrokerClient) and self.broker is not None:
            await self.tools.start()
        logger.info(f"Agent tools: {'MCP ' + ', '.join(self.tools.tools) if self.tools.available else self.tools.reason}")
        self.scheduler = AgentScheduler(
            self.event_store, self.task_queue, self.agent_runtime.active_agents,
            min_gap_seconds=float(os.getenv("FIRM_AGENT_MIN_GAP_MIN", "10")) * 60,
            routine_seconds=float(os.getenv("FIRM_AGENT_ROUTINE_MIN", "30")) * 60)
        if os.getenv("FIRM_NEWS", "1").strip().lower() not in ("0", "false", "no", "off"):
            from src.firm.data.news import NewsWire
            self.news = NewsWire(self.event_store)
            self._background_tasks.append(self._supervised("news_wire", lambda: self.news.run(lambda: self.is_running)))
        self._background_tasks += [
            self._supervised("casebook_loop", self.casebook_loop),
            self._supervised("llm_heartbeat_loop", self.llm_heartbeat_loop),
            self._supervised("scheduler_loop", self.scheduler_loop),
            self._supervised("agent_loop", self.agent_loop),
            self._supervised("hiring_loop", self.hiring_loop),
            self._supervised("learning_loop", self.learning_loop),
        ]
        mem = self.agent_runtime.memory
        logger.info(f"Gedaechtnis: {mem.count()} Lektionen in {Path(self.db_path).resolve()} "
                    f"(bleibt bei Neustart und Update erhalten; Ort: FIRM_HOME / firm memory status)")
        if os.getenv("FIRM_STUDIO", "1").strip().lower() not in ("0", "false", "no", "off"):
            self._background_tasks.append(self._supervised("studio_loop", self.studio_loop))

    async def learning_loop(self):
        """Lessons from measured results (live trades, shadow book, blocked signals, slippage) - no tokens.
        Confirmed while the numbers hold, replaced when they change (src/firm/agents/outcome_lessons.py)."""
        from src.firm.agents.outcome_lessons import refresh
        interval = float(os.getenv("FIRM_LEARN_SECONDS", "1800"))
        while self.is_running:
            try:
                stats = await asyncio.to_thread(refresh, self.event_store, self.agent_runtime.memory)
                if stats["added"] or stats["replaced"]:
                    logger.info(f"Learned from results: {stats}")
                    self.event_store.append("BRAIN", "OUTCOME_LESSONS", stats)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Learning from results failed: {e}")
            await asyncio.sleep(interval)

    def _studio_llm(self):
        """The studio may use a different (better) model than the agents: STUDIO_LLM_PROVIDER / STUDIO_MODEL."""
        provider, model = os.getenv("STUDIO_LLM_PROVIDER", "").strip(), os.getenv("STUDIO_MODEL", "").strip()
        if not provider and not model:
            return self.llm
        from src.firm.llm.factory import create_llm_provider
        return create_llm_provider(provider_name=provider or getattr(self.llm, "provider_name", None), model=model or None)

    async def studio_loop(self):
        """Studio: one workshop step at a time (idea, draft, audit). Products only reach AWAITING_APPROVAL -
        publishing is the operator's decision. Limits: config/limits.toml [studio] (defaults in studio/config.py)."""
        from src.firm.studio.catalog import Catalog
        from src.firm.studio.config import load_config, load_limits
        from src.firm.studio.workshop import Workshop
        interval = float(os.getenv("FIRM_STUDIO_SECONDS", "300"))
        workshop = Workshop(self._studio_llm(), Catalog(self.event_store.db_path, self.event_store), self.event_store,
                            load_config(), load_limits(), Path(os.getenv("FIRM_STUDIO_DIR") or data_dir() / "studio" / "products"),
                            firm_token_budget=int(self.limits.get("operational", {}).get("max_token_budget_daily", 0)) or None)
        logger.info(f"Studio started (one step every {interval:.0f}s; limits {workshop.limits}).")
        failures = 0
        while self.is_running:
            try:
                done = await workshop.tick()
                failures = 0
                if done:
                    logger.info(f"Studio: {done}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                logger.error(f"Studio step failed ({failures} in a row): {e}")
                if failures in (1, 5) or failures % 20 == 0:     # LLM down: report, but do not flood
                    self.event_store.append("STUDIO", "STUDIO_ERROR", {"step": "tick", "error": f"{type(e).__name__}: {str(e)[:200]}",
                                                                       "in_a_row": failures})
            # growing pause after errors (LLM/Internet down), at most 1 h
            await asyncio.sleep(interval if not failures else min(3600.0, interval * 2 ** min(failures, 6)))

    def _supervised(self, name: str, factory) -> asyncio.Task:
        """Background loop that never dies silently: an exception (or an unexpected return) is logged, written as
        TASK_DIED (dashboard + phone) and the loop restarts with growing back-off. Cancellation = normal stop."""
        async def runner():
            backoff = 5.0
            while self.is_running:
                try:
                    await factory()
                    if not self.is_running:
                        return
                    error = "returned unexpectedly"
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    error = f"{type(e).__name__}: {str(e)[:300]}"
                logger.critical(f"Background task {name} died ({error}) - restarting in {backoff:.0f}s")
                try:
                    self.event_store.append("SYSTEM", "TASK_DIED", {"task": name, "error": error,
                                                                    "restart_in_s": backoff})
                except Exception:
                    pass
                await asyncio.sleep(backoff)
                backoff = min(300.0, backoff * 2)
        return asyncio.create_task(runner(), name=name)

    async def _run_until_halt(self):
        logger.info(f"Firm is now LIVE (Mode: {self.current_mode}, zones: {', '.join(sorted(self.zones))}).")
        try:
            while self.is_running:
                # Operator halt via CLI/dashboard or a limit breach: `python -m src.firm halt`
                if HALT_FILE.exists():
                    if "money" in self.zones:
                        logger.critical(f"HALT flag detected ({HALT_FILE}). Activating kill switch and stopping.")
                        await self._kill("halt flag")
                    else:
                        logger.critical(f"HALT flag detected ({HALT_FILE}). Brain zone stopping.")
                    self.is_running = False
                    break
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    def _sync_registry(self) -> None:
        from src.firm.agents.registry import write_agents
        try:
            write_agents(self.db_path, self.agent_runtime.active_agents.values())
        except Exception as e:
            logger.warning(f"Agent registry not written: {e}")

    async def hiring_loop(self):
        """Agents approved in the dashboard (another process) are loaded here. Also the brain's heartbeat."""
        beat = 0.0
        while self.is_running:
            try:
                if asyncio.get_running_loop().time() - beat > 300:
                    self._sync_registry()             # updated_at = Lebenszeichen fuer /health/deep
                    beat = asyncio.get_running_loop().time()
                for req in self.ceo.approved_unapplied():
                    if req.role not in self.agent_runtime.active_agents:
                        self.hire_role(req.role)
                    self.ceo.mark_applied(req.request_id)
            except Exception as e:
                logger.error(f"Hiring loop error: {e}")
            await asyncio.sleep(10)

    async def casebook_loop(self):
        """Archivist's deterministic half: every closed trade becomes a case file with its context (JOURNAL_ENTRY)."""
        from src.firm.archive.casebook import Casebook
        book = Casebook(self.event_store, os.getenv("FIRM_ARCHIVE_DIR", "archiv"))
        while self.is_running:
            try:
                new = await asyncio.to_thread(book.sync)
                if new:
                    logger.info(f"Casebook: {len(new)} new trade case(s) archived.")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Casebook error: {e}")
            await asyncio.sleep(float(os.getenv("FIRM_CASEBOOK_SECONDS", "60")))

    async def llm_heartbeat_loop(self):
        """Is the agents' LLM reachable? Checked every minute without generating tokens; changes become an
        LLM_STATUS event (dashboard header, notifications). The agents retry on their own once it is back."""
        interval = float(os.getenv("FIRM_LLM_HEARTBEAT_SECONDS", "60"))
        last_ok: Optional[bool] = None
        while self.is_running:
            ok, error = True, None
            try:
                await asyncio.wait_for(self.llm.ping(), timeout=20)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                ok, error = False, f"{type(e).__name__}: {str(e)[:200]}"
                if getattr(self.llm, "provider_name", "") == "ollama":
                    from src.firm.ops.runtime_env import try_start_ollama
                    if try_start_ollama(getattr(self.llm, "base_url", "")):
                        error += " - 'ollama serve' wurde automatisch gestartet"
                    else:
                        error += " - Ollama starten: 'ollama serve' (oder Ollama-App oeffnen)"
            if ok != last_ok:
                self.event_store.append("BRAIN", "LLM_STATUS", {
                    "ok": ok, "provider": getattr(self.llm, "provider_name", "?"),
                    "model": getattr(self.llm, "model", None), "error": error})
                (logger.info if ok else logger.error)(f"LLM {'connected' if ok else 'unreachable: ' + str(error)}")
                last_ok = ok
            await asyncio.sleep(interval)

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
        requested = self.event_store.get_events(types=["RESEARCH_REQUESTED"], last=1)
        started = self.event_store.get_events(types=["RESEARCH_STARTED"], last=1)
        if requested and (not started or requested[-1]["ts"] > started[-1]["ts"]):
            return f"requested by {requested[-1].get('agent_id')}"
        shadow = self.shadow_research_due()
        if shadow:
            return shadow
        if self.gate.is_stale(max_age_days * 86400):
            return "no verdicts yet" if not self.gate.created() else f"verdicts older than {max_age_days:g} days"
        wanted = research_resolutions(self.trading_desk.resolution if self.trading_desk else "MINUTE_15")
        have = self.gate.overview().get("resolution") or []
        have = [have] if isinstance(have, str) else list(have)
        if sorted(have) != sorted(wanted):
            return f"timeframes changed: verdicts for {have}, configured {wanted}"
        return None

    async def run_research(self, why: str) -> None:
        if self._research_running or self.trading_desk is None:
            return
        if isinstance(self.broker, MockBrokerClient):
            logger.warning("Research skipped: no real broker connection (mock).")
            return
        self._research_running = True
        self._last_research_attempt = asyncio.get_running_loop().time()
        resolutions = research_resolutions(self.trading_desk.resolution)
        extra = self.scanner_candidates()
        epics = self.whitelist + extra
        self.event_store.append("RESEARCH_LAB", "RESEARCH_STARTED", {"why": why, "epics": epics,
                                                                     "scanner_candidates": extra,
                                                                     "resolutions": resolutions})
        logger.info(f"Research run started ({why}): {', '.join(epics)} {resolutions}")
        try:
            data_by_res = {}
            for res in resolutions:
                days = history_days(res)
                data_by_res[res] = await pipeline.fetch_history(self.broker, epics, res, days, log=logger.info)
            markets, account = await self._sizing_inputs()
            verdicts, lines, trials = await asyncio.to_thread(
                pipeline.run_research_multi, data_by_res, pipeline.previous_trials(self.gate.path), logger.info,
                markets, account)
            await asyncio.to_thread(pipeline.write_outputs, verdicts, lines, trials, resolutions, self.gate.path.parent)
            self.event_store.append("RESEARCH_LAB", "RESEARCH_DONE", {"trials": trials, "resolutions": resolutions,
                                                                      "summary": pipeline.summarize(verdicts)})
            logger.info(f"Research run done: {pipeline.summarize(verdicts)}")
            for epic in extra:
                v = verdicts.get(epic) or {}
                if v.get("verdict") in ("PASS", "FORWARD_TEST"):
                    self.event_store.append("RESEARCH_LAB", "WHITELIST_CANDIDATE", {
                        "epic": epic, "verdict": v.get("verdict"), "strategy": v.get("strategy"),
                        "resolution": v.get("resolution"), "oos_expectancy_r": v.get("oos_expectancy_r"),
                        "action": f"zum Handeln in config/whitelist.toml unter allowed_epics eintragen (Entscheidung "
                                  f"des Operators)"})
        except Exception as e:
            logger.error(f"Research run failed: {e}")
            self.event_store.append("RESEARCH_LAB", "RESEARCH_FAILED", {"error": str(e)[:500], "why": why})
        finally:
            self._research_running = False

    def shadow_promising(self) -> list:
        """Markets whose shadow-book results (scanner signals, spread deducted) clear the evidence bar."""
        from src.firm.research import shadow_stats
        events = self.event_store.get_events(types=["SHADOW_RESULT"], last=5000)
        verdicts = self.gate.overview().get("verdicts") or {}
        return shadow_stats.promising(shadow_stats.summarize(events), verdicts)

    def shadow_research_due(self) -> Optional[str]:
        """Shadow evidence triggers research for markets not covered by the last run (at most once a day)."""
        good = self.shadow_promising()
        if not good:
            return None
        import json as _json
        started = self.event_store.get_events(types=["RESEARCH_STARTED"], last=1)
        if started:
            last = started[-1]
            from datetime import datetime as _dt
            age = (_dt.utcnow() - _dt.fromisoformat(str(last["ts"]))).total_seconds()
            covered = set(_json.loads(last.get("payload_json") or "{}").get("epics") or [])
            if age < 86400 or all(g["epic"] in covered for g in good):
                return None
        g = good[0]
        return (f"shadow book: {g['epic']} {g['strategy']} {g['resolution']} {g['n']} signals, "
                f"{g['expectancy_r']:+.2f} R after spread")

    def scanner_candidates(self) -> list:
        """Top-K markets from the scanner's latest universe that are not on the whitelist (research only).
        More markets = more trials: the trial counter and deflated statistics in the lab account for that."""
        k = int(os.getenv("FIRM_RESEARCH_UNIVERSE", "6"))
        if k <= 0:
            return []
        ev = self.event_store.get_events(types=["UNIVERSE_UPDATED"], last=1)
        import json as _json
        shadow = [g["epic"] for g in self.shadow_promising() if g["epic"] not in self.whitelist]
        cands = (_json.loads(ev[-1]["payload_json"] or "{}").get("candidates") or []) if ev else []
        ranked = list(dict.fromkeys(shadow + [c["epic"] for c in cands if c.get("epic")]))
        return [e for e in ranked if e not in self.whitelist][:k]

    async def _sizing_inputs(self):
        """Real broker numbers for the minimum-size feasibility check (missing values -> check skipped)."""
        markets, account = {}, None
        try:
            acct = await self.broker.get_account_details()
            account = {"balance": float(acct.get("balance") or 0),
                       "risk_pct": float(self.trading_desk.limits["trade"]["max_risk_per_trade"])}
            for epic in self.whitelist + self.scanner_candidates():
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
        last_check_day = None
        while self.is_running:
            try:
                today = datetime.now(timezone.utc).date().isoformat()
                if last_check_day != today:
                    await self.run_promotion_check()
                    try:
                        from src.firm.ops.backup import backup_now
                        target = await asyncio.to_thread(backup_now, self.db_path, os.getenv("FIRM_BACKUP_DIR") or data_dir() / "backups",
                                                         int(os.getenv("FIRM_BACKUP_KEEP", "14")),
                                                         (str(data_dir() / "strategy_verdicts.json"),
                                                          os.getenv("FIRM_STATE_PATH", "firm_state.json")))
                        logger.info(f"Backup written: {target}")
                    except Exception as e:
                        logger.error(f"Backup failed: {e}")
                        self.event_store.append("SYSTEM", "BACKUP_FAILED", {"error": str(e)[:300]})
                    last_check_day = today
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

    async def run_promotion_check(self) -> dict:
        """Skalierungs-Leiter (Empfehlung an den Operator, nie automatisch)."""
        from src.firm.money.promotion import promotion_check
        live_bal = os.getenv("FIRM_LIVE_ACCOUNT_BALANCE")
        markets, _ = await self._sizing_inputs() if self.trading_desk is not None else ({}, None)
        result = await asyncio.to_thread(promotion_check, self.event_store, self.gate, self.limits, self.current_mode,
                                         float(live_bal) if live_bal else None, markets)
        self.event_store.append("SYSTEM", "PROMOTION_CHECK", result)
        return result

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
        self._sync_registry()
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
            last = self.event_store.get_events(types=["RESEARCH_STARTED"], last=1)
            done = self.event_store.get_events(types=["RESEARCH_DONE", "RESEARCH_FAILED"], last=1)
            if last and (not done or done[-1]["ts"] < last[-1]["ts"]):
                return {"status": "already running"}
            if not self.gate.is_stale(24 * 3600):
                return {"status": "declined", "reason": "verdicts are younger than 24 h"}
            self.event_store.append(agent, "RESEARCH_REQUESTED", {"reason": action["reason"]})
            return {"status": "queued"}
        if kind == "flag":
            self.event_store.append(agent, "FLAG", {"severity": action["severity"], "message": action["message"]})
            return {"status": "posted"}
        if kind == "propose_playbook":
            try:
                row = self.playbooks.propose(agent, agent, action["reason"], add_rule=action.get("add_rule"),
                                             remove_rule=action.get("remove_rule"))
            except ValueError as e:
                return {"status": "declined", "reason": str(e)}
            return {"status": "awaiting operator approval", "proposal_id": row["id"]}
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
        parallel = max(1, int(os.getenv("FIRM_AGENT_PARALLEL", "4")))
        gate = asyncio.Semaphore(parallel)
        logger.info(f"Agent loop started (parallel, max {parallel} at once).")
        while self.is_running:
            jobs = []
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
                if task:
                    jobs.append(self._run_agent_task(agent_name, task, gate))
            if jobs:
                await asyncio.gather(*jobs)
            await asyncio.sleep(interval)

    async def _run_agent_task(self, agent_name: str, task: dict, gate: asyncio.Semaphore) -> None:
        """One agent, one task. Agents work side by side (bounded by `gate`); one slow LLM answer no longer
        holds up the whole office. Money decisions still pass the single RiskGate in the money zone."""
        async with gate:
            try:
                observation = self._build_observation(agent_name, task)
                result = await self.agent_runtime.step(agent_name, observation, task["task_id"],
                                                       facts=self._facts(agent_name), on_action=self.handle_agent_action)
                self.task_queue.complete(task["task_id"], result)
                self._archive_task(agent_name, task, result=result)
                logger.info(f"Completed task {task['task_id']} for {agent_name}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Agent {agent_name} failed task {task['task_id']}: {e}")
                self.task_queue.fail(task["task_id"], str(e))
                self._archive_task(agent_name, task, error=str(e))

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
        interval = float(os.getenv("FIRM_TRADE_SECONDS", "60"))
        timeout = float(os.getenv("FIRM_TRADE_TIMEOUT", "120"))
        idle_hours = float(os.getenv("FIRM_IDLE_ALERT_HOURS", "12"))
        logger.info(f"Trading loop started (every {interval:.0f}s).")
        cycle = 0
        loop = asyncio.get_running_loop()
        last_activity = loop.time()
        while self.is_running:
            cycle += 1
            try:
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
            except Exception as e:
                logger.error(f"Idle watchdog error: {e}")
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
            await self._sleep_until_next_bar(interval)

    def _desk_resolutions(self) -> set:
        res = {self.trading_desk.resolution} if self.trading_desk is not None else set()
        for v in (self.gate.overview().get("verdicts") or {}).values():
            if isinstance(v, dict) and v.get("resolution"):
                res.add(v["resolution"])
        return {r for r in res if r in RES_SECONDS}

    async def _sleep_until_next_bar(self, max_interval: float) -> None:
        """Wake ~2 s after the next candle closes on any traded timeframe (signals use closed candles only),
        at the latest after `max_interval` seconds. Checked every second."""
        now = time.time()
        wake = now + max_interval
        try:
            resolutions = self._desk_resolutions()
        except Exception as e:
            logger.error(f"Desk timeframes unavailable ({e}); waking every {max_interval:.0f}s")
            resolutions = set()
        for r in resolutions:
            bar = RES_SECONDS[r]
            wake = min(wake, (now // bar + 1) * bar + 2.0)
        while self.is_running and time.time() < wake:
            await asyncio.sleep(min(1.0, max(0.05, wake - time.time())))

    async def account_snapshot_loop(self):
        """Publish real account snapshots (with open positions) for the dashboard."""
        interval = float(os.getenv("FIRM_ACCOUNT_SECONDS", "60"))
        while self.is_running:
            try:
                details = await self.broker.get_account_details()
                if isinstance(details, dict):
                    snapshot = dict(details)
                    snapshot["source"] = "mock" if isinstance(self.broker, MockBrokerClient) else "broker"
                    if self.stream is not None:
                        snapshot["stream"] = self.stream.status()
                    if self.scanner is not None:
                        snapshot["scanner"] = self.scanner.status()
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
        if getattr(self, "news", None) is not None:
            await self.news.close()
        if self.agent_runtime is not None:
            await self.agent_runtime.close()
        await self.tools.close()
        if self.reconciler:
            self.reconciler.stop()
        if self.broker:
            await self.broker.close()

    async def _kill(self, reason: str) -> None:
        """Kill switch + audit event (flat confirmed by the broker or not). Never raises: a failing kill must still
        be recorded and still stop the zone."""
        flat = None
        try:
            flat = await self.kill_switch.activate() if self.kill_switch else None
        except Exception as e:
            logger.critical(f"KILL-SWITCH raised: {e}")
        try:
            self.event_store.append("KILL_SWITCH", "KILL_SWITCH", {"reason": reason, "flat": flat})
        except Exception as e:
            logger.error(f"KILL_SWITCH event not written: {e}")

    async def handle_mismatch(self):
        logger.critical("RECONCILER MISMATCH DETECTED! Triggering KillSwitch...")
        try:
            await self._kill("reconciler mismatch")
        finally:
            self.is_running = False
