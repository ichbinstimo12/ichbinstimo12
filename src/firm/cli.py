import argparse
import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from src.firm.main import FirmOrchestrator, HALT_FILE, env_secret

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
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
        data = json.loads(LIVE_TOKEN_FILE.read_text(encoding="utf-8"))
        remaining = float(data.get("expires_at", 0)) - time.time()
        if remaining > 0:
            return f"VALID ({remaining / 60:.0f} min left)"
        return "EXPIRED"
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
async def main():
    parser = argparse.ArgumentParser(description="Autonomous Trading Firm CLI")
    subparsers = parser.add_subparsers(dest="command")

    # 'run' command
    run_parser = subparsers.add_parser("run", help="Start the trading firm")
    run_parser.add_argument("--mode", choices=["BACKTEST", "PAPER", "LIVE_MICRO", "LIVE"], default=os.getenv("FIRM_MODE", "PAPER"), help="Execution mode")
    run_parser.add_argument("--dashboard", action="store_true", help="Also serve the live office dashboard")
    run_parser.add_argument("--host", default="127.0.0.1", help="Dashboard bind host")
    run_parser.add_argument("--port", type=int, default=8000, help="Dashboard port")
    run_parser.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch oeffnen")

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
        if HALT_FILE.exists():
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

        logger.info(f"Starting firm in {args.mode} mode...")
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

        try:
            await orchestrator.start(mode=args.mode)
        except KeyboardInterrupt:
            logger.info("Shutting down via KeyboardInterrupt...")
        finally:
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
        expires_at = time.time() + args.duration
        token = {
            "created_at": datetime.utcnow().isoformat(),
            "expires_at": expires_at,
            "duration_seconds": args.duration,
            "account_hint": (env_secret("CAPITAL_IDENTIFIER") or "not-configured")[:60],
        }
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
