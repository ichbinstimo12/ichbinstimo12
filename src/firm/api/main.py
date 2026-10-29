import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.firm.bus.event_store import EventStore
from src.firm.money.strategy_gate import StrategyGate
from src.firm.agents.task_queue import TaskQueue

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FirmAPI")

ROOT_DIR = Path(__file__).resolve().parents[3]
WEB_DIR = ROOT_DIR / "web"
HALT_FILE = Path(os.getenv("FIRM_HALT_FILE", "firm.halt"))

DEFAULT_ORIGINS = "http://127.0.0.1:8000,http://localhost:8000"


class ConnectionManager:
    """Manages WebSocket connections for the Live Office Dashboard."""
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"New dashboard connection. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"Dashboard disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: Dict[str, Any]):
        if not self.active_connections:
            return
        payload = json.dumps(message, default=str)
        await asyncio.gather(
            *[conn.send_text(payload) for conn in list(self.active_connections)],
            return_exceptions=True,
        )


def create_app(
    event_store: Optional[EventStore] = None,
    task_queue: Optional[TaskQueue] = None,
    orchestrator: Any = None,
    api_token: Optional[str] = None,
    allowed_origins: Optional[List[str]] = None,
) -> FastAPI:
    store = event_store or EventStore(os.getenv("FIRM_DB_PATH", "firm_events.db"))
    queue = task_queue or TaskQueue(store.db_path, store)
    token = api_token if api_token is not None else os.getenv("FIRM_API_TOKEN", "")
    if token and token.strip() in ("", "HIER_EINFUEGEN"):
        token = ""
    origins = allowed_origins or [
        o.strip() for o in os.getenv("FIRM_ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",") if o.strip()
    ]

    app = FastAPI(title="Trading Firm Office API")
    manager = ConnectionManager()
    app.state.manager = manager

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )

    def require_token(request: Request) -> None:
        """If FIRM_API_TOKEN is configured, all API calls need a Bearer token."""
        if not token:
            return
        header = request.headers.get("authorization", "")
        provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not provided or not secrets.compare_digest(provided, token):
            raise HTTPException(status_code=401, detail="Invalid or missing API token")

    def latest_snapshot() -> Optional[Dict[str, Any]]:
        for event in reversed(store.get_events(types=["ACCOUNT_SNAPSHOT"], last=1)):
            if event["type"] == "ACCOUNT_SNAPSHOT":
                try:
                    return json.loads(event["payload_json"])
                except (json.JSONDecodeError, TypeError):
                    return None
        return None

    archive_cache: Dict[str, Any] = {"at": 0.0, "stats": None}
    # In split mode (supervisor) there is no orchestrator in this process: everything comes from the shared DB/files.
    _local: Dict[str, Any] = {}

    def _get(key: str, factory):
        if key not in _local:
            _local[key] = factory()
        return _local[key]

    def archive_obj() -> Any:
        if getattr(orchestrator, "archive", None) is not None:
            return orchestrator.archive
        from src.firm.archive.store import Archive
        return _get("archive", lambda: Archive(os.getenv("FIRM_ARCHIVE_DIR", "archiv")))

    def memory_obj() -> Any:
        mem = getattr(getattr(orchestrator, "agent_runtime", None), "memory", None)
        if mem is not None:
            return mem
        from src.firm.agents.memory import LessonStore
        return _get("memory", lambda: LessonStore(store.db_path))

    def ceo_obj() -> Any:
        if getattr(orchestrator, "ceo", None) is not None:
            return orchestrator.ceo
        from src.firm.agents.ceo_manager import CEOManager
        return _get("ceo", lambda: CEOManager(store))

    def agent_configs() -> List[Dict[str, Any]]:
        runtime = getattr(orchestrator, "agent_runtime", None)
        if runtime is not None:
            from dataclasses import asdict
            return [asdict(c) for c in runtime.active_agents.values() if c is not None]
        from src.firm.agents.registry import read_agents
        return read_agents(store.db_path)

    def whitelist() -> List[str]:
        import tomllib
        try:
            with open("config/whitelist.toml", "rb") as f:
                return list(tomllib.load(f).get("allowed_epics") or [])
        except OSError:
            return []

    async def cached_archive_stats(ttl: float = 15.0) -> Dict[str, Any]:
        """Archive stats walk the disk, so cache them briefly for the dashboard."""
        archive = archive_obj()
        if archive is None:
            return {}
        now = asyncio.get_event_loop().time()
        if archive_cache["stats"] is not None and (now - archive_cache["at"]) < ttl:
            return archive_cache["stats"]
        try:
            stats = await asyncio.to_thread(archive.stats)
        except Exception as e:
            logger.warning(f"Archive stats unavailable: {e}")
            return archive_cache["stats"] or {}
        archive_cache["stats"] = stats
        archive_cache["at"] = now
        return stats

    def strategy_payload() -> Dict[str, Any]:
        """Why the desk trades or not: research verdicts, desk reasons, agent risk overlays (all real data)."""
        gate = getattr(orchestrator, "gate", None) or StrategyGate()
        overlay = getattr(orchestrator, "overlay", None)
        status = store.get_events(types=["DESK_STATUS"], last=1)
        desk_status = None
        if status:
            try:
                desk_status = json.loads(status[-1]["payload_json"]) | {"since": status[-1]["ts"]}
            except (json.JSONDecodeError, TypeError):
                desk_status = None
        research = store.get_events(types=["RESEARCH_STARTED", "RESEARCH_DONE", "RESEARCH_FAILED"], last=1)
        ov = gate.overview()
        stopped = []
        for e in store.get_events(types=["STRATEGY_STOPPED"], last=50):
            try:
                pl = json.loads(e["payload_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            created = ov.get("created") or 0
            if datetime.fromisoformat(e["ts"]).timestamp() >= created - 7200:   # stops since the current verdicts
                stopped.append({"epic": pl.get("epic"), "strategy": pl.get("strategy"), "ts": e["ts"],
                                "reasons": pl.get("reasons")})
        return {
            "gate": ov,
            "stopped": stopped,
            "desk": desk_status,
            "research_last_event": ({"type": research[-1]["type"], "ts": research[-1]["ts"]} if research else None),
            "overlays": overlay.active() if overlay is not None else [],
            "mcp_tools": list(getattr(getattr(orchestrator, "tools", None), "tools", []) or []),
        }

    def status_payload() -> Dict[str, Any]:
        snapshot = latest_snapshot()
        desk = getattr(orchestrator, "trading_desk", None)
        mem = memory_obj()
        return {
            "status": "ONLINE",
            "timestamp": datetime.utcnow().isoformat(),
            "mode": getattr(orchestrator, "current_mode", None) or os.getenv("FIRM_MODE", "PAPER"),
            "llm_provider": os.getenv("LLM_PROVIDER", "ollama"),
            "llm_model": os.getenv("OLLAMA_MODEL" if os.getenv("LLM_PROVIDER", "ollama") == "ollama" else "ANTHROPIC_MODEL", ""),
            "balance": (snapshot or {}).get("balance"),
            "equity": (snapshot or {}).get("equity"),
            "profit_loss": (snapshot or {}).get("profit_loss"),
            "account_source": (snapshot or {}).get("source"),
            "positions": (snapshot or {}).get("positions") or [],
            "open_positions": (snapshot or {}).get("open_positions"),
            "trading": {
                "enabled": desk is not None if orchestrator is not None else os.getenv("FIRM_TRADING", "1") != "0",
                "epics": list(getattr(desk, "epics", []) or []) or whitelist(),
                "resolution": getattr(desk, "resolution", None) or os.getenv("FIRM_TRADE_RESOLUTION", "HOUR"),
            },
            "stream": (snapshot or {}).get("stream"),
            "snapshot_ts": snapshot_ts(),
            "today": today_payload(),
            "tasks": queue.count_by_status(),
            "lessons_learned": mem.count() if mem is not None else 0,
            "strategy": strategy_payload(),
            "kill_switch_active": bool(getattr(getattr(orchestrator, "kill_switch", None), "is_active", False))
            or HALT_FILE.exists(),
            "agents": [{"name": c["name"], "role": c["role"]} for c in agent_configs()],
            "alerts": alerts_payload(),
            "promotion": last_payload("PROMOTION_CHECK"),
            "agent_eval": last_payload("AGENT_EVAL"),
        }

    def last_payload(event_type: str) -> Optional[Dict[str, Any]]:
        ev = store.get_events(types=[event_type], last=1)
        if not ev:
            return None
        try:
            return dict(json.loads(ev[-1]["payload_json"]), ts=ev[-1]["ts"])
        except (json.JSONDecodeError, TypeError):
            return None

    ALERT_TYPES = ["TRADING_HALTED", "STRATEGY_STOPPED", "STRATEGY_WARNING", "BROKER_OFFLINE", "DESK_IDLE",
                   "RESEARCH_FAILED", "AGENT_INVALID", "LESSON_CONFLICT", "STREAM_DISCONNECTED", "FLAG",
                   "INJECTION_SUSPECTED", "CIRCUIT_BREAKER", "KILL_SWITCH", "BACKUP_FAILED"]

    def alerts_payload() -> List[Dict[str, Any]]:
        """Alarm-Leiste: die letzten Warnungen der letzten 24 Stunden (echte Events)."""
        since = (datetime.utcnow().replace(microsecond=0) - timedelta(hours=24)).isoformat()
        out = []
        for e in store.get_events(after_ts=since, types=ALERT_TYPES)[-12:]:
            try:
                pl = json.loads(e["payload_json"])
            except (json.JSONDecodeError, TypeError):
                pl = {}
            if e["type"] == "FLAG" and pl.get("severity") == "info":
                continue
            text = pl.get("reason") or pl.get("message") or pl.get("error") or "; ".join(pl.get("reasons") or []) \
                or pl.get("lesson") or ""
            out.append({"id": e["id"], "ts": e["ts"], "type": e["type"], "agent": e.get("agent_id"),
                        "text": str(text)[:200]})
        return out

    def snapshot_ts() -> Optional[str]:
        snaps = store.get_events(types=["ACCOUNT_SNAPSHOT"], last=1)
        return snaps[-1]["ts"] if snaps else None

    def today_payload() -> Dict[str, Any]:
        """Tages-PnL, Drawdown und Token-Verbrauch heute - alles aus echten Events (UTC-Tag)."""
        from src.firm.agents.metrics import tokens_today
        start = datetime.utcnow().strftime("%Y-%m-%dT00:00:00")
        eq = []
        for e in store.get_events(after_ts=start, types=["ACCOUNT_SNAPSHOT"]):
            try:
                v = json.loads(e["payload_json"]).get("equity")
            except (json.JSONDecodeError, TypeError):
                v = None
            if isinstance(v, (int, float)):
                eq.append(v)
        peak, dd = (eq[0] if eq else None), 0.0
        for v in eq:
            peak = max(peak, v)
            dd = max(dd, (peak - v) / peak if peak else 0.0)
        return {"pnl": (eq[-1] - eq[0]) if len(eq) > 1 else None,
                "drawdown_pct": dd * 100 if eq else None,
                "tokens": tokens_today(store)}

    # ---------------- HTTP API ----------------
    @app.get("/health")
    async def health():
        """Unauthenticated liveness probe (no firm data is exposed)."""
        return {"status": "ok"}

    @app.get("/health/deep")
    async def health_deep():
        """Zones alive? 200 = all fresh, 503 = something is down or halted. No account figures (safe to expose to
        an uptime monitor)."""
        from fastapi.responses import JSONResponse
        from src.firm.ops.health import zone_health
        h = zone_health(store, HALT_FILE, float(os.getenv("FIRM_HEALTH_MONEY_MAX_AGE", "300")))
        return JSONResponse(h, status_code=200 if h["ok"] else 503)

    @app.get("/api/status", dependencies=[Depends(require_token)])
    async def get_status():
        payload = status_payload()
        stats = await cached_archive_stats()
        if stats:
            payload["archive"] = {
                "root": stats.get("root"),
                "trades_total": stats.get("trades_total"),
                "trades_closed": stats.get("trades_closed"),
                "trades_with_profit": stats.get("trades_with_profit"),
                "realized_profit": stats.get("realized_profit"),
                "tasks_per_agent": stats.get("tasks_per_agent"),
                "compressed_files": stats.get("compressed_files"),
                "size_bytes": stats.get("size_bytes"),
            }
        return payload

    @app.get("/api/events", dependencies=[Depends(require_token)])
    async def get_events(limit: int = 100):
        limit = max(1, min(limit, 1000))
        return {"events": store.get_events(last=limit)}

    @app.get("/api/tasks", dependencies=[Depends(require_token)])
    async def get_tasks(status: Optional[str] = None):
        return {"tasks": queue.list_tasks(status=status)}

    @app.get("/api/agents", dependencies=[Depends(require_token)])
    async def get_agents():
        agents = []
        memory = memory_obj()
        from src.firm.agents.metrics import agent_metrics, tokens_today
        metrics = agent_metrics(store, days=7)
        for c in agent_configs():
            agents.append({
                "name": c["name"],
                "role": c["role"],
                "model": c.get("model"),
                "reporting_line": c.get("reporting_line"),
                "daily_token_budget": c.get("daily_token_budget"),
                "tools": list(c.get("tool_allowlist") or []),
                "lessons_learned": memory.count(c["name"]) if memory is not None else 0,
                "metrics_7d": metrics.get(c["name"]),
                "tokens_today": tokens_today(store, c["name"]),
                "actions": list(c.get("actions") or []),
                "triggers": list(c.get("triggers") or []),
            })
        return {"agents": agents}

    @app.get("/api/lessons", dependencies=[Depends(require_token)])
    async def get_lessons(agent: Optional[str] = None, limit: int = 20):
        """What the agents have learned from their own work (advisory notes only)."""
        memory = memory_obj()
        if agent:
            return {"lessons": memory.recent(agent, limit=limit)}
        lessons = []
        for c in agent_configs():
            lessons.extend(memory.recent(c["name"], limit=limit))
        return {"lessons": lessons}

    @app.get("/api/archive", dependencies=[Depends(require_token)])
    async def get_archive(limit: int = 100):
        """The filing cabinet: archive stats plus the real trade history."""
        archive = archive_obj()
        if archive is None:
            return {"available": False, "stats": {}, "trades": []}
        limit = max(1, min(limit, 500))
        stats = await cached_archive_stats()
        try:
            trades = await asyncio.to_thread(archive.trades, limit)
        except Exception as e:
            logger.warning(f"Could not read archived trades: {e}")
            trades = []
        return {"available": True, "stats": stats, "trades": trades}

    @app.get("/api/archive/trades", dependencies=[Depends(require_token)])
    async def get_archive_trades(limit: int = 200):
        archive = archive_obj()
        if archive is None:
            return {"trades": []}
        limit = max(1, min(limit, 1000))
        try:
            trades = await asyncio.to_thread(archive.trades, limit)
        except Exception as e:
            logger.warning(f"Could not read archived trades: {e}")
            trades = []
        return {"trades": trades}

    @app.get("/api/archive/agents/{agent}", dependencies=[Depends(require_token)])
    async def get_archive_agent(agent: str, limit: int = 20):
        """What one agent filed: latest tasks with the actions it took."""
        archive = archive_obj()
        if archive is None:
            return {"agent": agent, "tasks": []}
        limit = max(1, min(limit, 100))
        try:
            tasks = await asyncio.to_thread(archive.agent_tasks, agent, limit)
        except Exception as e:
            logger.warning(f"Could not read archive for {agent}: {e}")
            tasks = []
        return {"agent": agent, "tasks": tasks}

    @app.post("/api/halt", dependencies=[Depends(require_token)])
    async def halt():
        HALT_FILE.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
        if orchestrator is not None and getattr(orchestrator, "kill_switch", None) is not None:
            await orchestrator.kill_switch.activate()
        logger.critical("HALT requested via API.")
        await manager.broadcast({"type": "SYSTEM", "content": "KILL SWITCH activated via dashboard."})
        return {"status": "HALTED"}

    @app.post("/api/hire/{request_id}", dependencies=[Depends(require_token)])
    async def hire(request_id: str, decision: Dict[str, str]):
        ceo = ceo_obj()
        choice = (decision.get("decision") or "").upper()
        try:
            if choice == "APPROVED":
                ceo.approve_hire(request_id)
            elif choice == "REJECTED":
                ceo.reject_hire(request_id)
            else:
                raise HTTPException(status_code=400, detail="decision must be APPROVED or REJECTED")
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"request_id": request_id, "decision": choice}

    def playbook_store():
        from src.firm.agents.playbooks import PlaybookStore
        return getattr(orchestrator, "playbooks", None) or PlaybookStore(store.db_path, event_store=store)

    @app.get("/api/playbooks", dependencies=[Depends(require_token)])
    async def playbooks(role: Optional[str] = None):
        pbs = playbook_store()
        roles = [role] if role else sorted(p.stem for p in Path("config/playbooks").glob("*.md"))
        return {"active": {r: pbs.active(r) for r in roles}, "pending": pbs.pending()}

    @app.post("/api/playbooks/{proposal_id}", dependencies=[Depends(require_token)])
    async def decide_playbook(proposal_id: str, decision: Dict[str, str]):
        choice = (decision.get("decision") or "").upper()
        if choice not in ("APPROVED", "REJECTED"):
            raise HTTPException(status_code=400, detail="decision must be APPROVED or REJECTED")
        try:
            row = playbook_store().decide(proposal_id, choice == "APPROVED")
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"id": proposal_id, "status": row["status"], "version": row.get("version")}

    @app.delete("/api/lessons/{lesson_id}", dependencies=[Depends(require_token)])
    async def delete_lesson(lesson_id: str):
        from src.firm.agents.memory import LessonStore
        memory = memory_obj()
        if not memory.delete(lesson_id):
            raise HTTPException(status_code=404, detail="lesson not found")
        store.append("USER", "LESSON_DELETED", {"lesson_id": lesson_id})
        return {"deleted": lesson_id}

    @app.get("/api/hiring", dependencies=[Depends(require_token)])
    async def hiring():
        requests = ceo_obj().hiring_requests
        return {"requests": [
            {
                "request_id": r.request_id,
                "role": r.role,
                "justification": r.justification,
                "token_budget": r.token_budget,
                "status": r.status.value,
            }
            for r in requests.values()
        ]}

    # ---------------- Dashboard (same origin) ----------------
    @app.get("/")
    async def office():
        index = WEB_DIR / "office.html"
        if not index.exists():
            raise HTTPException(status_code=404, detail="Dashboard not found")
        return FileResponse(index)

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    # ---------------- WebSocket ----------------
    @app.websocket("/ws/office")
    async def office_websocket(websocket: WebSocket):
        # Browsers always send Origin on WebSockets; CORS does not cover them, so check it here
        # (blocks a foreign web page from riding a token it found in the URL/history).
        origin = websocket.headers.get("origin")
        if origin and origin not in origins:
            await websocket.accept()
            await websocket.close(code=4403)
            return
        if token:
            provided = websocket.query_params.get("token", "")
            if not provided or not secrets.compare_digest(provided, token):
                await websocket.accept()
                await websocket.close(code=4401)
                return

        await manager.connect(websocket)
        try:
            history = store.get_events(last=100)
            await websocket.send_json({
                "type": "SYNC",
                "data": history,
                "tasks": queue.list_tasks(limit=50),
                "status": status_payload(),
            })

            last_ts = history[-1]["ts"] if history else None

            async def poller():
                nonlocal last_ts
                while True:
                    await asyncio.sleep(0.5)
                    new_events = store.get_events(after_ts=last_ts) if last_ts else store.get_events(last=100)
                    for event in new_events:
                        await websocket.send_json({"type": "EVENT", "data": event})
                        last_ts = event["ts"]

            poll_task = asyncio.create_task(poller())
            try:
                while True:
                    message = await websocket.receive_text()
                    try:
                        msg = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type")
                    if msg_type == "KILL_SWITCH":
                        HALT_FILE.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
                        if orchestrator is not None and getattr(orchestrator, "kill_switch", None) is not None:
                            await orchestrator.kill_switch.activate()
                        await manager.broadcast({"type": "SYSTEM", "content": "KILL SWITCH activated."})
                    elif msg_type in ("APPROVE_HIRE", "REJECT_HIRE"):
                        ceo = ceo_obj()
                        request_id = msg.get("request_id")
                        if ceo is not None and request_id:
                            if msg_type == "APPROVE_HIRE":
                                ceo.approve_hire(request_id)
                            else:
                                ceo.reject_hire(request_id)
                            await manager.broadcast({"type": "SYSTEM", "content": f"{msg_type}: {request_id}"})
                        else:
                            await websocket.send_json({"type": "SYSTEM", "content": "Hiring decisions unavailable in this process."})
            finally:
                poll_task.cancel()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            manager.disconnect(websocket)

    return app


app = create_app()
