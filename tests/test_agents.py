import json

import pytest

from src.firm.agents.actions import extract_json, validate_action
from src.firm.agents.actions import testable_lesson as lesson_filter
from src.firm.agents.ceo_manager import CEOManager, RequestStatus
from src.firm.agents.facts import build_facts, check_claims
from src.firm.agents.runtime import AgentConfig, AgentRuntime
from src.firm.bus.event_store import EventStore
from src.firm.llm.base import LLMProvider, LLMResponse
from src.firm.money.risk_overlay import RiskOverlay

WHITELIST = ["BTCUSD", "EURUSD", "GOLD", "AAPL"]


class ScriptedLLM(LLMProvider):
    """Returns the scripted answers in order and records every call."""

    provider_name = "stub"
    model = "stub-model"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def generate(self, system_prompt, messages, max_tokens=None, temperature=None, json_mode=False):
        self.calls.append({"system": system_prompt, "messages": messages, "json_mode": json_mode})
        content = self.answers.pop(0) if self.answers else '{"summary": "nothing to add", "actions": []}'
        if not isinstance(content, str):
            content = json.dumps(content)
        return LLMResponse(content=content, model="stub-model", provider="stub")

    async def close(self):
        pass


class FailingLLM(LLMProvider):
    provider_name = "stub"

    async def generate(self, system_prompt, messages, max_tokens=None, temperature=None):
        raise RuntimeError("ollama down")

    async def close(self):
        pass


class FakeTools:
    available = True
    tools = ["cap_market_get", "cap_market_sentiment"]

    def __init__(self):
        self.calls = []

    def describe(self, allowed=None):
        return "\n".join(f"- {t}" for t in self.tools if allowed is None or t in allowed)

    async def call(self, name, args=None, allowed=None):
        self.calls.append((name, args, allowed))
        if allowed is not None and name not in allowed:
            return {"ok": False, "error": "not allowed"}
        return {"ok": True, "result": '{"snapshot": {"marketStatus": "TRADEABLE", "bid": 2400.1, "offer": 2400.5}}'}


@pytest.fixture
def event_store(tmp_path):
    return EventStore(str(tmp_path / "test_agents.db"))


def _config(name="RiskOfficer", actions=("tighten_risk", "block_entries", "flag"), tools=("cap_market_get",)):
    return AgentConfig(name=name, role="Risk Officer", system_prompt="You watch risk.",
                       tool_allowlist=list(tools), actions=list(actions))


def _facts(open_positions=0, balance=1000.0):
    return {"mode": "PAPER", "account": {"balance": balance, "equity": balance, "currency": "CHF"},
            "open_positions": open_positions, "live_stats": {"closed_trades": 0}}


def _types(store):
    return [e["type"] for e in store.get_events()]


# ------------------------------------------------------------------ runtime
@pytest.mark.asyncio
async def test_valid_answer_is_recorded_and_uses_json_mode(event_store):
    llm = ScriptedLLM({"summary": "Flat book, nothing to do.", "claims": {"open_positions": 0}, "actions": []})
    runtime = AgentRuntime(event_store, llm=llm, whitelist=WHITELIST)
    runtime.active_agents["RiskOfficer"] = _config()
    result = await runtime.step("RiskOfficer", "Check risk", "task-1", facts=_facts())
    assert result["status"] == "success"
    assert result["deliverable"] == "Flat book, nothing to do."
    assert llm.calls[0]["json_mode"] is True
    assert "FACTS" in llm.calls[0]["messages"][0]["content"]
    assert _types(event_store)[:2] == ["THOUGHT", "RESULT"]


@pytest.mark.asyncio
async def test_claim_contradicting_facts_is_rejected(event_store):
    """The old agents wrote 'no exposure' while a position was open. That answer must be thrown away."""
    llm = ScriptedLLM({"summary": "No exposure.", "claims": {"open_positions": 0},
                       "actions": [{"type": "flag", "severity": "info", "reason": "all quiet on the book"}],
                       "lesson": "GOLD is always flat on Mondays at 10:00"})
    runtime = AgentRuntime(event_store, llm=llm, whitelist=WHITELIST)
    runtime.active_agents["RiskOfficer"] = _config()
    result = await runtime.step("RiskOfficer", "Check risk", "task-2", facts=_facts(open_positions=1))
    assert result["status"] == "invalid"
    assert result["actions"] == [] and result["lessons_learned"] == []
    assert "AGENT_INVALID" in _types(event_store)
    assert runtime.memory.count("RiskOfficer") == 0


@pytest.mark.asyncio
async def test_free_text_answer_is_invalid(event_store):
    # free text twice (also after the one format correction) stays invalid
    llm = ScriptedLLM("I think the market looks bullish.", "Still bullish, no JSON.")
    runtime = AgentRuntime(event_store, llm=llm, whitelist=WHITELIST)
    runtime.active_agents["RiskOfficer"] = _config()
    result = await runtime.step("RiskOfficer", "Check", "task-3", facts=_facts())
    assert result["status"] == "invalid" and len(llm.calls) == 2


@pytest.mark.asyncio
async def test_actions_go_through_handler_and_disallowed_ones_are_rejected(event_store, tmp_path):
    overlay = RiskOverlay(event_store.db_path, event_store)

    async def handler(agent, action):
        row = overlay.tighten(action["epic"], action["factor"], action["hours"], agent, action["reason"])
        return {"status": "applied", "factor": row["factor"]}

    llm = ScriptedLLM({"summary": "Two losses in GOLD with wide spreads.", "actions": [
        {"type": "tighten_risk", "epic": "GOLD", "factor": 0.5, "hours": 12, "reason": "two stop-outs with spread > 0.1 ATR"},
        {"type": "request_research", "reason": "role is not allowed to request research"},
        {"type": "tighten_risk", "epic": "GOLD", "factor": 2.0, "hours": 12, "reason": "trying to raise risk is impossible"},
    ]})
    runtime = AgentRuntime(event_store, llm=llm, whitelist=WHITELIST)
    runtime.active_agents["RiskOfficer"] = _config()
    result = await runtime.step("RiskOfficer", "Check", "task-4", facts=_facts(), on_action=handler)
    assert [a["action"]["type"] for a in result["actions"]] == ["tighten_risk"]
    assert overlay.factor("GOLD")[0] == 0.5
    assert overlay.factor("EURUSD")[0] == 1.0
    assert _types(event_store).count("ACTION_REJECTED") == 2


@pytest.mark.asyncio
async def test_tool_loop_uses_only_allowed_tools(event_store):
    tools = FakeTools()
    llm = ScriptedLLM({"tool": "cap_market_get", "args": {"epic": "GOLD"}},
                      {"tool": "cap_market_sentiment", "args": {"market_id": "GOLD"}},
                      {"summary": "GOLD tradeable, spread 0.4.", "actions": []})
    runtime = AgentRuntime(event_store, llm=llm, tools=tools, whitelist=WHITELIST)
    runtime.active_agents["RiskOfficer"] = _config(tools=("cap_market_get",))
    result = await runtime.step("RiskOfficer", "Check", "task-5", facts=_facts())
    assert result["status"] == "success" and result["tool_calls"] == 2
    assert tools.calls[0][2] == ["cap_market_get"]
    tool_events = [json.loads(e["payload_json"]) for e in event_store.get_events() if e["type"] == "TOOL_CALL"]
    assert [t["ok"] for t in tool_events] == [True, False]
    assert "TOOL RESULT" in llm.calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_only_testable_lessons_are_stored_and_applied_later(event_store):
    llm = ScriptedLLM(
        {"summary": "ok", "actions": [], "lesson": "I should have been more careful."},
        {"summary": "ok", "actions": [], "lesson": "GOLD donchian_trend entries with spread above 0.1 ATR were rejected twice."},
        {"summary": "ok", "actions": []},
    )
    runtime = AgentRuntime(event_store, llm=llm, whitelist=WHITELIST)
    runtime.active_agents["RiskOfficer"] = _config()
    first = await runtime.step("RiskOfficer", "Check", "t-1", facts=_facts())
    assert first["lessons_learned"] == []
    second = await runtime.step("RiskOfficer", "Check", "t-2", facts=_facts())
    assert len(second["lessons_learned"]) == 1
    third = await runtime.step("RiskOfficer", "Check", "t-3", facts=_facts())
    assert third["lessons_applied"] == 1
    assert "donchian_trend" in llm.calls[2]["system"] and "advisory notes only" in llm.calls[2]["system"]


@pytest.mark.asyncio
async def test_no_learning_from_llm_failure(event_store):
    runtime = AgentRuntime(event_store, llm=FailingLLM(), whitelist=WHITELIST)
    runtime.active_agents["RiskOfficer"] = _config()
    result = await runtime.step("RiskOfficer", "Anything", "task-x", facts=_facts())
    assert result["deliverable"].startswith("[LLM_UNAVAILABLE]")
    assert runtime.memory.count("RiskOfficer") == 0
    assert "LEARNED" not in _types(event_store)


def test_role_model_is_ignored_on_the_wrong_provider(event_store):
    """Role files said `model: llama3.1:8b`; on Anthropic that produced a 404 for every agent call."""
    llm = ScriptedLLM()
    llm.provider_name = "anthropic"
    runtime = AgentRuntime(event_store, llm=llm)
    cfg = _config()
    cfg.model = "llama3.1:8b"
    assert runtime._provider_for(cfg) is llm


# ------------------------------------------------------------------ actions
def test_validate_action_can_only_reduce_risk():
    allowed = ["tighten_risk", "block_entries", "flag"]
    ok, _ = validate_action({"type": "tighten_risk", "epic": "GOLD", "factor": 1.5, "hours": 4,
                             "reason": "more risk please, it looks good"}, allowed, WHITELIST)
    assert not ok
    ok, _ = validate_action({"type": "tighten_risk", "epic": "TSLA", "factor": 0.5, "hours": 4,
                             "reason": "not in the whitelist market"}, allowed, WHITELIST)
    assert not ok
    ok, _ = validate_action({"type": "block_entries", "epic": "EURUSD", "hours": 4,
                             "reason": "rate decision at 14:00 UTC today"}, allowed, WHITELIST)
    assert not ok, "a block without a source is rejected"
    ok, norm = validate_action({"type": "block_entries", "epic": "EURUSD", "hours": 99, "source": "cap_market_get",
                                "reason": "market closed for holiday"}, allowed, WHITELIST)
    assert not ok
    ok, norm = validate_action({"type": "place_order", "epic": "GOLD", "reason": "buy gold now please"}, allowed, WHITELIST)
    assert not ok


def test_overlay_clamps_and_takes_the_minimum(tmp_path):
    store = EventStore(str(tmp_path / "o.db"))
    clock = [1000.0]
    ov = RiskOverlay(store.db_path, store, now_fn=lambda: clock[0])
    ov.tighten("GOLD", 5.0, 1000, "X", "factor above one is clamped")
    ov.tighten("*", 0.5, 2, "Y", "global caution for two hours")
    assert ov.factor("GOLD")[0] == 0.5
    clock[0] += 3 * 3600
    assert ov.factor("GOLD")[0] == 1.0
    assert "RISK_TIGHTENED" in [e["type"] for e in store.get_events()]


def test_extract_json_handles_fences_and_prose():
    assert extract_json('Sure!\n```json\n{"summary": "a {b}", "x": 1}\n```') == {"summary": "a {b}", "x": 1}
    assert extract_json("no json here") is None


def test_testable_lesson_filter():
    assert lesson_filter("We should always double-check everything carefully.", WHITELIST) is None
    assert lesson_filter("NONE", WHITELIST) is None
    assert lesson_filter("EURUSD entries during the Asian session were all rejected by the spread filter.", WHITELIST)


# ------------------------------------------------------------------ facts
def test_facts_come_from_the_event_log(event_store):
    event_store.append("SYSTEM", "ACCOUNT_SNAPSHOT", {"balance": 995.17, "equity": 990.0, "currency": "CHF",
                                                     "positions": [{"epic": "GOLD", "direction": "BUY"}]})
    event_store.append("TRADING_DESK", "DESK_STATUS", {"error": None, "reasons": {"GOLD": "position already open"}})
    facts = build_facts(event_store, "PAPER")
    assert facts["account"]["balance"] == 995.17
    assert facts["open_positions"] == 1
    assert facts["desk"]["why_no_entry"] == {"GOLD": "position already open"}
    assert check_claims({"open_positions": 0}, facts)
    assert check_claims({"open_positions": 1, "balance": 995.17}, facts) == []


def test_ceo_hiring_flow(event_store):
    ceo = CEOManager(event_store)
    req_id = ceo.create_hiring_request("RiskOfficer", "Need better drawdown monitoring", {"drawdown_freq": "high"}, 5000)
    assert ceo.hiring_requests[req_id].status == RequestStatus.PENDING
    ceo.approve_hire(req_id)
    assert ceo.hiring_requests[req_id].status == RequestStatus.APPROVED


def test_saying_unknown_is_not_a_false_number():
    from src.firm.agents.facts import check_claims
    facts = {"account": {"balance": 1000.0, "equity": 1000.0}, "open_positions": 0}
    assert check_claims({"balance": "UNKNOWN", "equity": None}, facts) == []
    assert check_claims({"balance": 1200}, facts) == ["balance: behauptet 1200, Fakt ist 1000"]
    assert check_claims({"balance": 5}, {"account": "UNKNOWN (kein Snapshot)"}) == ["balance: behauptet 5, Fakt ist UNBEKANNT"]
