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


def test_routine_keeps_every_agent_working_one_after_another(tmp_path):
    store = EventStore(str(tmp_path / "s.db"))
    queue = TaskQueue(store.db_path, store)
    clock = [10_000.0]
    sched = AgentScheduler(store, queue, _agents(), now_fn=lambda: clock[0], min_gap_seconds=300,
                           routine_seconds=1800, startup_roles=())
    assert len(sched.tick()) == 1 and len(sched.tick()) == 1 and len(sched.tick()) == 1   # staggered start
    assert _assigned(queue) == ["CEO", "HeadOfResearch", "RiskOfficer"]
    assert sched.tick() == []                                    # all busy / recently worked
    for t in queue.list_tasks(limit=100):
        queue.complete(t["task_id"], {"status": "success"})
    clock[0] += 1000
    assert sched.tick() == []                                    # routine not yet due
    clock[0] += 900
    assert len(sched.tick()) == 1                                # 30 min later: next round


@pytest.mark.asyncio
async def test_llm_heartbeat_reports_only_state_changes(tmp_path, monkeypatch):
    import asyncio
    import json
    from src.firm import main as firm_main
    orch = firm_main.FirmOrchestrator.__new__(firm_main.FirmOrchestrator)
    orch.event_store = EventStore(str(tmp_path / "h.db"))
    orch.is_running = True
    states = iter([None, None, ConnectionError("refused"), None])

    class LLM:
        provider_name, model = "ollama", "qwen2.5:14b"

        async def ping(self):
            err = next(states, None)
            if err:
                raise err
    orch.llm = LLM()
    monkeypatch.setenv("FIRM_LLM_HEARTBEAT_SECONDS", "0.01")
    task = asyncio.create_task(orch.llm_heartbeat_loop())
    await asyncio.sleep(0.2)
    orch.is_running = False
    await asyncio.wait_for(task, 2)
    ev = [json.loads(e["payload_json"]) for e in orch.event_store.get_events(types=["LLM_STATUS"])]
    assert [e["ok"] for e in ev] == [True, False, True] and "refused" in ev[1]["error"]


def test_idle_firm_gives_agents_self_directed_work(tmp_path):
    from src.firm.agents.delegation import IDLE_WORK
    store = EventStore(str(tmp_path / "s.db"))
    queue = TaskQueue(store.db_path, store)
    store.append("TRADING_DESK", "DESK_STATUS", {"reasons": {"GOLD": "keine Forschungsfreigabe"}})
    sched = AgentScheduler(store, queue, _agents(), now_fn=lambda: 10_000.0, min_gap_seconds=60,
                           routine_seconds=600, startup_roles=())
    assert "keine Forschungsfreigabe" in sched.firm_idle()
    sched.tick()
    t = queue.list_tasks(limit=10)[0]
    assert "self-directed work" in t["description"] and IDLE_WORK[t["assigned_to"]][0][:40] in t["description"]
