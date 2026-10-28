import asyncio
import json
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.firm.bus.event_store import EventStore
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
        allow_methods=["GET", "POST"],
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
        for event in reversed(store.get_events()):
            if event["type"] == "ACCOUNT_SNAPSHOT":
                try:
                    return json.loads(event["payload_json"])
                except (json.JSONDecodeError, TypeError):
                    return None
        return None

    archive_cache: Dict[str, Any] = {"at": 0.0, "stats": None}

    def archive_obj() -> Any:
        return getattr(orchestrator, "archive", None)

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

    def status_payload() -> Dict[str, Any]:
        snapshot = latest_snapshot()
        desk = getattr(orchestrator, "trading_desk", None)
        return {
            "status": "ONLINE",
            "timestamp": datetime.utcnow().isoformat(),
            "mode": getattr(orchestrator, "current_mode", os.getenv("FIRM_MODE", "PAPER")),
            "llm_provider": os.getenv("LLM_PROVIDER", "ollama"),
            "llm_model": os.getenv("OLLAMA_MODEL" if os.getenv("LLM_PROVIDER", "ollama") == "ollama" else "ANTHROPIC_MODEL", ""),
            "balance": (snapshot or {}).get("balance"),
            "equity": (snapshot or {}).get("equity"),
            "profit_loss": (snapshot or {}).get("profit_loss"),
            "account_source": (snapshot or {}).get("source"),
            "positions": (snapshot or {}).get("positions") or [],
            "open_positions": (snapshot or {}).get("open_positions"),
            "trading": {
                "enabled": desk is not None,
                "epics": list(getattr(desk, "epics", []) or []),
                "resolution": getattr(desk, "resolution", None),
            },
            "tasks": queue.count_by_status(),
            "lessons_learned": (
                getattr(getattr(orchestrator, "agent_runtime", None), "memory", None).count()
                if getattr(getattr(orchestrator, "agent_runtime", None), "memory", None) is not None
                else 0
            ),
            "kill_switch_active": bool(getattr(getattr(orchestrator, "kill_switch", None), "is_active", False)),
            "agents": [
                {"name": c.name, "role": c.role}
                for c in getattr(getattr(orchestrator, "agent_runtime", None), "active_agents", {}).values()
                if c is not None
            ],
        }

    # ---------------- HTTP API ----------------
    @app.get("/health")
    async def health():
        """Unauthenticated liveness probe (no firm data is exposed)."""
        return {"status": "ok"}

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
        return {"events": store.get_events()[-limit:]}

    @app.get("/api/tasks", dependencies=[Depends(require_token)])
    async def get_tasks(status: Optional[str] = None):
        return {"tasks": queue.list_tasks(status=status)}

    @app.get("/api/agents", dependencies=[Depends(require_token)])
    async def get_agents():
        agents = []
        memory = getattr(getattr(orchestrator, "agent_runtime", None), "memory", None)
        for c in getattr(getattr(orchestrator, "agent_runtime", None), "active_agents", {}).values():
            if c is None:
                continue
            agents.append({
                "name": c.name,
                "role": c.role,
                "model": c.model,
                "reporting_line": c.reporting_line,
                "daily_token_budget": c.daily_token_budget,
                "lessons_learned": memory.count(c.name) if memory is not None else 0,
            })
        return {"agents": agents}

    @app.get("/api/lessons", dependencies=[Depends(require_token)])
    async def get_lessons(agent: Optional[str] = None, limit: int = 20):
        """What the agents have learned from their own work (advisory notes only)."""
        memory = getattr(getattr(orchestrator, "agent_runtime", None), "memory", None)
        if memory is None:
            return {"lessons": []}
        if agent:
            return {"lessons": memory.recent(agent, limit=limit)}
        lessons = []
        for name in getattr(getattr(orchestrator, "agent_runtime", None), "active_agents", {}):
            lessons.extend(memory.recent(name, limit=limit))
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
        ceo = getattr(orchestrator, "ceo", None)
        if ceo is None:
            raise HTTPException(status_code=409, detail="CEO manager not available in this API process")
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

    @app.get("/api/hiring", dependencies=[Depends(require_token)])
    async def hiring():
        requests = getattr(getattr(orchestrator, "ceo", None), "hiring_requests", {}) or {}
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
        if token:
            provided = websocket.query_params.get("token", "")
            if not provided or not secrets.compare_digest(provided, token):
                await websocket.accept()
                await websocket.close(code=4401)
                return

        await manager.connect(websocket)
        try:
            history = store.get_events()[-100:]
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
                    new_events = store.get_events(after_ts=last_ts) if last_ts else store.get_events()
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
                        ceo = getattr(orchestrator, "ceo", None)
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
