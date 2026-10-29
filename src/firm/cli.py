import argparse
import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from src.firm.envload import load_env

from src.firm.main import FirmOrchestrator, HALT_FILE, env_secret

load_env()
logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
# httpx logs every request URL at INFO; Telegram bot tokens, the secret ntfy topic and healthcheck UUIDs are part
# of those URLs. Secrets never go to the logs.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger("FirmCLI")

LIVE_TOKEN_FILE = Path(os.getenv("FIRM_LIVE_TOKEN_FILE", "live_token.json"))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
async def _broker_status():
    """Return (status line, open positions) from the configured broker."""
    from src.firm.broker.client import CapitalComClient

    key = env_secret("CAPITAL_API_KEY")
    identifier = env_secret("CAPITAL_IDENTIFIER")
    password = env_secret("CAPITAL_PASSWORD")
    if not (key and identifier and password):
        return "NOT CONFIGURED (add Capital.com demo keys to .env)", []

    client = CapitalComClient(key, identifier, password)
    try:
        if await asyncio.wait_for(client.authenticate(), timeout=20):
            details = await asyncio.wait_for(client.get_account_details(), timeout=20)
            try:
                positions = await asyncio.wait_for(client.get_positions(), timeout=20)
            except Exception:
                positions = []
            return f"ONLINE - {json.dumps(details)[:160]}", positions
        return "OFFLINE (authenticate returned false)", []
    except Exception as e:
        return f"OFFLINE ({e})", []
    finally:
        with contextlib.suppress(Exception):
            await client.close()


def _live_token_state() -> str:
    if not LIVE_TOKEN_FILE.exists():
        return "none"
    try:
        from src.firm.money.live_approval import verify
        data = json.loads(LIVE_TOKEN_FILE.read_text(encoding="utf-8"))
        ok, why = verify(data, os.environ, "config/limits.toml")
        remaining = float(data.get("expires_at", 0)) - time.time()
        return f"VALID ({remaining / 60:.0f} min left)" if ok else f"INVALID ({why})"
    except Exception:
        return "INVALID"


async def _status():
    from src.firm.bus.event_store import EventStore
    from src.firm.agents.task_queue import TaskQueue
    from src.firm.agents.memory import LessonStore

    db_path = os.getenv("FIRM_DB_PATH", "firm_events.db")
    store = EventStore(db_path)
    queue = TaskQueue(db_path, store)
    events = store.get_events()

    provider = os.getenv("LLM_PROVIDER", "ollama")
    model = os.getenv("OLLAMA_MODEL" if provider == "ollama" else "ANTHROPIC_MODEL", "-")

    snapshot = None
    for event in reversed(events):
        if event["type"] == "ACCOUNT_SNAPSHOT":
            try:
                snapshot = json.loads(event["payload_json"])
                break
            except (json.JSONDecodeError, TypeError):
                continue

    print("\nTRADING FIRM - STATUS")
    print("=" * 56)
    broker_line, positions = await _broker_status()
    print(f"Mode              : {os.getenv('FIRM_MODE', 'PAPER')}")
    print(f"LLM               : {provider} / {model}")
    print(f"Events (total)    : {len(events)}")
    print(f"Tasks             : {queue.count_by_status()}")
    print(f"Lessons learned   : {LessonStore(db_path).count()}")
    print(f"Halt flag         : {'ACTIVE' if HALT_FILE.exists() else 'off'}")
    print(f"Live approval     : {_live_token_state()}")
    if snapshot:
        print(f"Balance           : {snapshot.get('balance')} {snapshot.get('currency', '')} (source: {snapshot.get('source', '?')})")
    else:
        print("Balance           : - (no account snapshot yet)")
    print(f"Broker            : {broker_line}")
    if positions:
        print(f"Open positions    : {len(positions)}")
        for p in positions:
            upl = p.get("upl")
            upl_txt = f"{upl:+.2f}" if isinstance(upl, (int, float)) else "?"
            print(
                f"  - {p.get('epic')} {p.get('direction')} {p.get('volume')} "
                f"@ {p.get('level')}  P/L {upl_txt}"
            )
    else:
        print("Open positions    : 0 (flat)")
    print("=" * 56 + "\n")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
async def _doctor() -> int:
    print("\nFIRM DOCTOR")
    print("=" * 56)
    ok = True

    print(f"{'[OK]' if Path('.env').exists() else '[MISSING]'} .env")

    broker_configured = True
    for var in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_PASSWORD"):
        present = env_secret(var) is not None
        broker_configured = broker_configured and present
        print(f"{'[OK]' if present else '[OPTIONAL]'} {var} (Capital.com demo)")
    if not broker_configured:
        print("         -> missing broker keys: the firm runs with the MOCK broker (PAPER)")

    provider = os.getenv("LLM_PROVIDER", "ollama")
    if provider == "ollama":
        import httpx
        base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
        try:
            response = httpx.get(f"{base}/api/tags", timeout=5.0)
            response.raise_for_status()
            names = [m.get("name") for m in response.json().get("models", [])]
            print(f"[OK] Ollama reachable at {base}")
            if model in names:
                print(f"[OK] model '{model}' available")
            else:
                base_name = model.split(":")[0]
                matches = [n for n in names if n and n.split(":")[0] == base_name]
                if matches:
                    print(f"[OK] model '{model}' resolvable via {matches}")
                else:
                    print(f"[MISSING] model '{model}' not pulled. Run: ollama pull {model}")
                    print(f"         available: {', '.join(n for n in names if n) or 'none'}")
                    ok = False
        except Exception as e:
            print(f"[MISSING] Ollama not reachable at {base}: {e}")
            ok = False
    elif provider == "anthropic":
        present = env_secret("ANTHROPIC_API_KEY") is not None
        print(f"{'[OK]' if present else '[MISSING]'} ANTHROPIC_API_KEY")
        ok = ok and present
    else:
        print(f"[MISSING] Unknown LLM_PROVIDER '{provider}' (use ollama or anthropic)")
        ok = False

    try:
        import tomllib
        from src.firm.money.limits_check import limit_warnings
        with open("config/limits.toml", "rb") as f:
            limits = tomllib.load(f)
        warnings = limit_warnings(limits)
        print(f"{'[OK]' if not warnings else '[WARN]'} config/limits.toml: Risiko je Trade "
              f"{float(limits['trade']['max_risk_per_trade']):.1%}")
        for w in warnings:
            print(f"       - {w}")
    except Exception as e:
        print(f"[MISSING] config/limits.toml unreadable: {e}")
        ok = False

    roles_dir = Path(os.getenv("FIRM_ROLES_DIR", "config/roles"))
    role_count = len(list(roles_dir.glob("*.yaml"))) if roles_dir.exists() else 0
    print(f"{'[OK]' if role_count else '[MISSING]'} role configs in {roles_dir} ({role_count})")
    ok = ok and role_count > 0

    print("=" * 56)
    if ok:
        print("Core checks passed." + ("" if broker_configured else " Broker runs in MOCK mode until Capital.com demo keys are set."))
    else:
        print("Some checks failed - see above.")
    print()
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
async def _run_zone(args) -> int:
    """One isolated zone process (started by the supervisor)."""
    import signal
    if args.name == "api":
        import uvicorn
        from src.firm.api.main import create_app
        server = uvicorn.Server(uvicorn.Config(create_app(), host=args.host, port=args.port, log_level="info"))
        await server.serve()
        return 0
    if args.name == "notify":
        return await _run_notify()
    if args.name == "meme":
        return await _run_meme()
    if HALT_FILE.exists():
        logger.error("Halt flag active - zone %s not started.", args.name)
        return 0
    orchestrator = FirmOrchestrator()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):  # Windows: no signal handlers in asyncio
        loop.add_signal_handler(signal.SIGTERM, lambda: setattr(orchestrator, "is_running", False))
    await orchestrator.start(mode=args.mode, zones=(args.name,))
    return 0


async def _wait_while_halted(args) -> bool:
    """Halt active: keep only the read-only dashboard up until the flag is removed (the halt message itself was
    already pushed by the notify zone before it stopped; the dead-man's switch reports the halted state).
    Returns True when the operator ran `resume`, False on SIGTERM."""
    import signal
    logger.critical("HALT flag '%s' active - nothing trades. Waiting for: python -m src.firm resume", HALT_FILE)
    stop = asyncio.Event()
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        loop.add_signal_handler(signal.SIGINT, stop.set)
    server = server_task = None
    if args.dashboard:
        import uvicorn
        from src.firm.api.main import create_app
        server = uvicorn.Server(uvicorn.Config(create_app(), host=args.host, port=args.port, log_level="warning"))
        server_task = asyncio.create_task(server.serve())
    try:
        while HALT_FILE.exists() and not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5)
    finally:
        if server is not None:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server_task, timeout=10)
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop = asyncio.get_running_loop()           # hand SIGTERM back to whatever runs next
            loop.remove_signal_handler(signal.SIGTERM)
            loop.remove_signal_handler(signal.SIGINT)
    if stop.is_set():
        return False
    logger.info("Halt flag removed - starting the firm again.")
    return True


async def _run_meme() -> int:
    """Memecoin desk zone (PAPER by default). Halt flag of the firm stops it too."""
    import signal
    from src.firm.bus.event_store import EventStore
    from src.firm.memedesk.desk import MemeDesk
    from src.firm.memedesk.logic import load_config
    from src.firm.memedesk.sources import MemeSources
    stop = asyncio.Event()
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    cfg = load_config(override=Path("data/memedesk/params_current.json"))
    src = MemeSources(cfg)
    desk = MemeDesk(src, EventStore(os.getenv("FIRM_DB_PATH", "firm_events.db")), cfg=cfg)
    try:
        await desk.run(lambda: not stop.is_set() and not HALT_FILE.exists())
    finally:
        desk.save()
        await src.close()
    return 0


def _meme_command(args) -> int:
    """meme status | kill | resume | coach | live  (operator commands for the memecoin desk)."""
    from src.firm.bus.event_store import EventStore
    from src.firm.memedesk import live_gate
    from src.firm.memedesk.logic import load_config, load_limits
    data = Path("data/memedesk")
    store = EventStore(os.getenv("FIRM_DB_PATH", "firm_events.db"))
    if args.action == "kill":
        data.mkdir(parents=True, exist_ok=True)
        (data / "halt").write_text(datetime.utcnow().isoformat(), encoding="utf-8")
        store.append("OPERATOR", "MEME_KILL_REQUESTED", {"source": "cli"})
        print("Kill-Switch gesetzt: der Memecoin-Desk schliesst alle Positionen und stoppt.")
    elif args.action == "resume":
        (data / "halt").unlink(missing_ok=True)
        st = data / "state.json"
        if st.exists():
            state = json.loads(st.read_text(encoding="utf-8"))
            state["halted"] = None
            st.write_text(json.dumps(state, indent=1), encoding="utf-8")
        store.append("OPERATOR", "MEME_RESUMED", {"source": "cli"})
        print("Memecoin-Desk freigegeben (Kill-Switch/Drawdown-Stopp aufgehoben).")
    elif args.action == "coach":
        from src.firm.memedesk.coach import run_coach
        res = run_coach(load_config(override=data / "params_current.json"), Path("archiv/memedesk/journal.jsonl"),
                        data, store)
        print(json.dumps(res, indent=1, default=str))
    elif args.action == "live":
        ok, why = live_gate.check(Path("archiv/memedesk/journal.jsonl"), load_limits(), data / "state.json")
        store.append("OPERATOR", "MEME_MODE_REQUEST", {"mode": "LIVE", "granted": False, "reason": why})
        print(("Kriterien erfuellt, aber " if ok else "LIVE nicht freigeschaltet: ") + why)
        return 1
    else:
        st = data / "state.json"
        print(st.read_text(encoding="utf-8") if st.exists() else "Memecoin-Desk hat noch keinen Zustand (noch nicht gestartet).")
    return 0


async def _run_notify() -> int:
    """Notification zone: reads the event log, pushes to ntfy/Telegram, pings the dead-man's switch."""
    import signal
    from src.firm.bus.event_store import EventStore
    from src.firm.ops.notify import Notifier
    notifier = Notifier()
    if not notifier.configured:
        logger.info("No NOTIFY_*/HEALTHCHECK_PING_URL set - notification zone not started.")
        return 0
    stop = asyncio.Event()
    with contextlib.suppress(NotImplementedError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    try:
        await notifier.run(EventStore(os.getenv("FIRM_DB_PATH", "firm_events.db")), HALT_FILE, stop=stop)
    finally:
        await notifier.close()
    return 0


async def _notify_test() -> int:
    from src.firm.ops.notify import Notifier
    notifier = Notifier()
    if not notifier.configured:
        print("Keine Benachrichtigung konfiguriert. Setze NOTIFY_NTFY_URL oder NOTIFY_TELEGRAM_BOT_TOKEN + "
              "NOTIFY_TELEGRAM_CHAT_ID (und optional HEALTHCHECK_PING_URL) in .env.")
        return 1
    try:
        ok = await notifier.send("Test", "Benachrichtigungen der Trading-Firma funktionieren.", "default")
        await notifier.ping(True)
    finally:
        await notifier.close()
    print("Testnachricht gesendet." if ok else "Senden fehlgeschlagen - URL/Token pruefen (Details im Log).")
    return 0 if ok else 1


async def main():
    parser = argparse.ArgumentParser(description="Autonomous Trading Firm CLI")
    subparsers = parser.add_subparsers(dest="command")

    # 'run' command
    run_parser = subparsers.add_parser("run", help="Start the trading firm")
    run_parser.add_argument("--mode", choices=["BACKTEST", "PAPER", "LIVE_MICRO", "LIVE"], default=os.getenv("FIRM_MODE", "PAPER"), help="Execution mode")
    run_parser.add_argument("--dashboard", action="store_true", help="Also serve the live office dashboard")
    run_parser.add_argument("--host", default="127.0.0.1", help="Dashboard bind host")
    run_parser.add_argument("--port", type=int, default=8000, help="Dashboard port")
    run_parser.add_argument("--wait-on-halt", action="store_true",
                            default=os.getenv("FIRM_WAIT_ON_HALT", "0") == "1",
                            help="Bei Halt-Flag nicht beenden, sondern warten (Dashboard bleibt), bis 'resume' laeuft")
    run_parser.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch oeffnen")
    run_parser.add_argument("--single", action="store_true",
                            help="Alles in einem Prozess (ohne Prozesstrennung; z. B. zum Debuggen)")

    # 'zone' - one isolated process, started by the supervisor
    zone_parser = subparsers.add_parser("zone", help="(intern) eine isolierte Zone; startet der Supervisor")
    zone_parser.add_argument("name", choices=["money", "brain", "api", "notify", "meme"])
    zone_parser.add_argument("--mode", default=os.getenv("FIRM_MODE", "PAPER"))
    zone_parser.add_argument("--host", default="127.0.0.1")
    zone_parser.add_argument("--port", type=int, default=8000)

    # 'eval' - measured agent quality (ECC eval-harness style)
    eval_parser = subparsers.add_parser("eval", help="Agenten-Evals: feste Szenarien gegen das konfigurierte LLM (pass@k)")
    eval_parser.add_argument("--runs", type=int, default=3)
    eval_parser.add_argument("--scenario", default=None)

    subparsers.add_parser("notify-test", help="Testnachricht an ntfy/Telegram senden und Healthcheck pingen")
    meme_parser = subparsers.add_parser("meme", help="Memecoin-Desk: status | kill | resume | coach | live")
    meme_parser.add_argument("action", choices=["status", "kill", "resume", "coach", "live"], nargs="?", default="status")

    # 'halt' / 'resume'
    subparsers.add_parser("halt", help="Emergency halt of all operations (kill switch + stop)")
    subparsers.add_parser("resume", help="Clear the halt flag so the firm can start again")

    # 'status'
    subparsers.add_parser("status", help="Check firm health, tasks and balance")

    # 'doctor'
    subparsers.add_parser("doctor", help="Check .env, LLM and role configuration")

    # 'approve-live' command
    approve_parser = subparsers.add_parser("approve-live", help="Generate a time-limited live-execution token")
    approve_parser.add_argument("--duration", type=int, default=3600, help="Token validity in seconds")

    # 'llm' command - switch the Brain-Zone LLM provider (ollama <-> anthropic)
    llm_parser = subparsers.add_parser("llm", help="Manage the LLM provider of the Brain-Zone")
    llm_parser.add_argument("--provider", required=True, choices=["ollama", "anthropic"], help="LLM provider to use")

    args = parser.parse_args()

    if args.command == "run":
        # A stale halt flag must stop the run before anything is initialised.
        if HALT_FILE.exists() and not args.wait_on_halt:
            logger.error(
                "Halt flag '%s' is active. Resolve the incident, then run: python -m src.firm resume",
                HALT_FILE,
            )
            return 1

        # Safety: never expose the dashboard on a non-loopback interface without a token.
        if args.dashboard and args.host not in ("127.0.0.1", "localhost", "::1"):
            if not env_secret("FIRM_API_TOKEN"):
                logger.error(
                    "Refusing to bind the dashboard to %s without FIRM_API_TOKEN. "
                    "Set a real token in .env or bind to 127.0.0.1.",
                    args.host,
                )
                return 1

        # Server (24/7): with a halt flag the firm waits - dashboard up, nothing trades - until you run `resume`.
        if HALT_FILE.exists() and args.wait_on_halt:
            if not await _wait_while_halted(args):
                return 0

        if not args.single:
            from src.firm.supervisor import Supervisor
            logger.info(f"Starting firm in {args.mode} mode as separate processes (money / brain / mcp / api)...")
            sup = Supervisor(args.mode, args.dashboard, args.host, args.port, HALT_FILE)
            if args.no_browser:
                sup.base_env["FIRM_OPEN_BROWSER"] = "0"
            rc = await sup.run()
            while args.wait_on_halt and HALT_FILE.exists() and not sup.terminated:
                if not await _wait_while_halted(args):
                    return rc
                sup = Supervisor(args.mode, args.dashboard, args.host, args.port, HALT_FILE)
                sup.base_env["FIRM_OPEN_BROWSER"] = "0"
                rc = await sup.run()
            return rc

        logger.info(f"Starting firm in {args.mode} mode (single process)...")
        orchestrator = FirmOrchestrator()

        server = None
        server_task = None
        browser_task = None
        if args.dashboard:
            import uvicorn
            from src.firm.api.main import create_app

            api_app = create_app(
                event_store=orchestrator.event_store,
                task_queue=orchestrator.task_queue,
                orchestrator=orchestrator,
            )
            server = uvicorn.Server(uvicorn.Config(api_app, host=args.host, port=args.port, log_level="info"))
            server_task = asyncio.create_task(server.serve())
            logger.info(f"Dashboard: http://{args.host}:{args.port}/")
            if not args.no_browser and os.getenv("FIRM_OPEN_BROWSER", "1") != "0":
                from src.firm.api.launcher import open_when_ready
                browser_task = asyncio.create_task(open_when_ready(args.host, args.port, os.getenv("FIRM_API_TOKEN", "")))

        from src.firm.ops.notify import Notifier
        notifier = Notifier()
        notify_stop = asyncio.Event()
        notify_task = (asyncio.create_task(notifier.run(orchestrator.event_store, HALT_FILE, stop=notify_stop))
                       if notifier.configured else None)
        meme_task = (asyncio.create_task(_run_meme())
                     if os.getenv("FIRM_MEMEDESK", "1").strip().lower() not in ("0", "false", "no", "off") else None)
        try:
            await orchestrator.start(mode=args.mode)
        except KeyboardInterrupt:
            logger.info("Shutting down via KeyboardInterrupt...")
        finally:
            if meme_task is not None:
                meme_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await meme_task
            if notify_task is not None:
                notify_stop.set()
                with contextlib.suppress(asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(notify_task, timeout=20)
            await notifier.close()
            if server is not None:
                server.should_exit = True
            if server_task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(server_task), timeout=5)
                except (asyncio.TimeoutError, Exception):
                    server_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await server_task
            await orchestrator.stop()

    elif args.command == "eval":
        from src.firm.agents.evals import run_all
        from src.firm.llm.factory import get_llm_provider
        llm = get_llm_provider()
        try:
            report = await run_all(llm, runs=args.runs, only=args.scenario)
        finally:
            await llm.close()
        for r in report["scenarios"]:
            print(f"{r['name']:<28} pass@{r['runs']}={'JA ' if r['pass_at_k'] else 'NEIN'} "
                  f"pass^{r['runs']}={'JA ' if r['pass_all_k'] else 'NEIN'} rate={r['pass_rate']:.0%}")
            for o in r["outcomes"]:
                if o["fails"]:
                    print(f"    Lauf {o['run']}: {'; '.join(o['fails'])}")
        rate = report["pass_rate"]
        print(f"Gesamt: {rate:.0%} ({report['provider']} {report['model']}) -> {report['path']}" if rate is not None
              else "Keine Szenarien gefunden (evals/agents/*.yaml)")
        try:
            EventStore(os.getenv("FIRM_DB_PATH", "firm_events.db")).append(
                "EVAL", "AGENT_EVAL", {"pass_rate": rate, "model": report["model"], "provider": report["provider"],
                                       "scenarios": {r["name"]: r["pass_rate"] for r in report["scenarios"]},
                                       "path": report["path"]})
        except Exception:
            pass

    elif args.command == "zone":
        return await _run_zone(args)

    elif args.command == "meme":
        return _meme_command(args)

    elif args.command == "notify-test":
        return await _notify_test()

    elif args.command == "halt":
        HALT_FILE.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
        logger.critical(f"HALT flag written to {HALT_FILE}. A running firm stops within ~1s and flattens positions.")
        logger.info("After you have resolved the issue, run: python -m src.firm resume")

    elif args.command == "resume":
        if HALT_FILE.exists():
            HALT_FILE.unlink()
            logger.info("Halt flag removed. The firm can be started again.")
        else:
            logger.info("No halt flag present.")

    elif args.command == "status":
        await _status()

    elif args.command == "doctor":
        return await _doctor()

    elif args.command == "approve-live":
        from src.firm.money.live_approval import issue
        try:
            token = issue(args.duration, os.environ, "config/limits.toml")
        except RuntimeError as e:
            logger.error(str(e))
            return 1
        LIVE_TOKEN_FILE.write_text(json.dumps(token, indent=2), encoding="utf-8")
        logger.warning(f"LIVE approval token written to {LIVE_TOKEN_FILE} for {args.duration}s. Use only with --mode LIVE_MICRO/LIVE.")

    elif args.command == "llm":
        # Switch the Brain-Zone LLM provider (needs firm restart to take full effect)
        from src.firm.llm import create_llm_provider

        if args.provider == "ollama":
            os.environ["LLM_PROVIDER"] = "ollama"
            os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")
            os.environ.setdefault("OLLAMA_MODEL", "llama3.1:8b")
        elif args.provider == "anthropic":
            os.environ["LLM_PROVIDER"] = "anthropic"
            os.environ.setdefault("ANTHROPIC_MODEL", "claude-opus-5-5")
            if not env_secret("ANTHROPIC_API_KEY"):
                logger.error("ANTHROPIC_API_KEY is not set. Add it to .env before switching to anthropic.")
                return

        create_llm_provider(args.provider)
        logger.info(f"LLM provider switched to '{args.provider}'. Restart the firm (python -m src.firm run) for all agents to pick it up.")
    else:
        parser.print_help()


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()) or 0)
