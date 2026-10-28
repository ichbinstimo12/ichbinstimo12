"""Ereignisgesteuerte Aufgaben und die Sicherheitsgrenzen des Capital.com-MCP-Clients."""
import pytest

from src.firm.agents.delegation import AgentScheduler
from src.firm.agents.runtime import AgentConfig
from src.firm.agents.task_queue import TaskQueue
from src.firm.bus.event_store import EventStore
from src.firm.tools.capital_mcp import READ_ONLY_TOOLS, CapitalMCPTools


def _agents():
    mk = lambda name, triggers, hours=0: AgentConfig(name=name, role=name, system_prompt="x", tool_allowlist=[],
                                                     triggers=triggers, schedule_hours=hours)
    return {"CEO": mk("CEO", ["DESK_IDLE"], 24), "RiskOfficer": mk("RiskOfficer", ["TRADE_RESULT"], 6),
            "HeadOfResearch": mk("HeadOfResearch", ["RESEARCH_DONE"])}


def _setup(tmp_path):
    store = EventStore(str(tmp_path / "s.db"))
    queue = TaskQueue(store.db_path, store)
    clock = [10_000.0]
    sched = AgentScheduler(store, queue, _agents(), now_fn=lambda: clock[0])
    return store, queue, sched, clock


def _assigned(queue):
    return sorted(t["assigned_to"] for t in queue.list_tasks(limit=100))


def test_nothing_happens_without_events_except_the_start_briefing(tmp_path):
    store, queue, sched, clock = _setup(tmp_path)
    store.append("SYSTEM", "ACCOUNT_SNAPSHOT", {"balance": 1})
    sched.tick()
    assert _assigned(queue) == ["CEO", "RiskOfficer"]
    for _ in range(10):                      # the old loop created a task per agent every 60 s
        clock[0] += 60
        store.append("SYSTEM", "ACCOUNT_SNAPSHOT", {"balance": 1})
        sched.tick()
    assert len(queue.list_tasks(limit=100)) == 2


def test_events_wake_only_the_roles_that_listen(tmp_path):
    store, queue, sched, clock = _setup(tmp_path)
    sched._startup_pending = []
    sched.tick()
    store.append("RESEARCH_LAB", "RESEARCH_DONE", {"trials": 5})
    sched.tick()
    tasks = queue.list_tasks(limit=100)
    assert [t["assigned_to"] for t in tasks] == ["HeadOfResearch"]
    assert "RESEARCH_DONE" in tasks[0]["description"]


def test_one_open_task_per_role_and_periodic_reviews(tmp_path):
    store, queue, sched, clock = _setup(tmp_path)
    sched._startup_pending = []
    sched.tick()
    store.append("TRADING_DESK", "TRADE_RESULT", {"pnl": -4})
    sched.tick()
    clock[0] += 3600
    store.append("TRADING_DESK", "TRADE_RESULT", {"pnl": -4})
    sched.tick()
    assert _assigned(queue) == ["RiskOfficer"], "still pending -> no second task"
    clock[0] += 24 * 3600 + 1
    sched.tick()
    assert "CEO" in _assigned(queue)


def test_mcp_client_blocks_every_write_tool_without_asking_the_server():
    tools = CapitalMCPTools()
    for name in ("cap_trade_execute_position", "cap_trade_preview_position", "cap_trade_positions_close",
                 "cap_account_demo_topup", "cap_account_preferences_set", "cap_watchlists_delete"):
        assert name not in READ_ONLY_TOOLS
        import asyncio
        out = asyncio.run(tools.call(name, {"confirm": True}))
        assert out["ok"] is False and "gesperrt" in out["error"]


def test_mcp_server_env_forces_demo_and_disables_trading(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPITAL_API_KEY", "k")
    monkeypatch.setenv("CAPITAL_IDENTIFIER", "i")
    monkeypatch.setenv("CAPITAL_PASSWORD", "p")
    monkeypatch.setenv("CAPITAL_ENV", "live")
    env = CapitalMCPTools(workdir=tmp_path)._server_env()
    assert env["CAP_ENV"] == "demo"
    assert env["CAP_ALLOW_TRADING"] == "false" and env["CAP_DRY_RUN"] == "true"
    assert "ANTHROPIC_API_KEY" not in env


@pytest.mark.asyncio
async def test_mcp_role_allowlist(monkeypatch):
    tools = CapitalMCPTools()
    out = await tools.call("cap_market_sentiment", {"market_id": "GOLD"}, allowed=["cap_market_get"])
    assert out["ok"] is False and "Rechten" in out["error"]
