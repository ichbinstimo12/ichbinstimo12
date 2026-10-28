import pytest
import asyncio
from src.firm.bus.event_store import EventStore
from src.firm.agents.runtime import AgentRuntime, AgentConfig
from src.firm.agents.ceo_manager import CEOManager, RequestStatus
from src.firm.llm.base import LLMProvider, LLMResponse


class StubLLM(LLMProvider):
    provider_name = "stub"

    async def generate(self, system_prompt, messages, max_tokens=None, temperature=None):
        return LLMResponse(content="stub thought", model="stub", provider="stub")

    async def close(self):
        pass


class RecordingLLM(LLMProvider):
    """Returns a deliverable, and lessons when asked to reflect."""

    provider_name = "stub"
    model = "stub-model"

    def __init__(self):
        self.calls = []

    async def generate(self, system_prompt, messages, max_tokens=None, temperature=None):
        self.calls.append({"system": system_prompt, "messages": messages})
        joined = " ".join(str(m.get("content") or "") for m in messages)
        if "Reflect: what should you check" in joined:
            return LLMResponse(
                content=(
                    "LESSON: Verify data freshness before drawing any conclusion.\n"
                    "LESSON: Check position concentration before proposing new exposure."
                ),
                model="stub-model",
                provider="stub",
            )
        return LLMResponse(content="Concrete deliverable: check data, then report.", model="stub-model", provider="stub")

    async def close(self):
        pass


class FailingLLM(LLMProvider):
    provider_name = "stub"

    async def generate(self, system_prompt, messages, max_tokens=None, temperature=None):
        raise RuntimeError("ollama down")

    async def close(self):
        pass


@pytest.fixture
def event_store(tmp_path):
    db_file = tmp_path / "test_agents.db"
    return EventStore(str(db_file))


def _config(name="QuantDev", role="Developer"):
    return AgentConfig(
        name=name,
        role=role,
        system_prompt="You build strategies.",
        tool_allowlist=["get_data"],
        model="llama3.1:8b",
    )


@pytest.mark.asyncio
async def test_agent_runtime_loop(event_store):
    runtime = AgentRuntime(event_store, llm=StubLLM())
    runtime.active_agents["QuantDev"] = _config()

    result = await runtime.step("QuantDev", "Market is bullish", "task-123")
    assert result["status"] == "success"
    assert result["deliverable"] == "stub thought"

    events = event_store.get_events(correlation_id="task-123")
    types = [e["type"] for e in events]
    assert types[0] == "THOUGHT"
    assert "RESULT" in types
    # No fabricated tool output in the audit trail
    assert "TOOL_RESULT" not in types
    assert "60000" not in str(events)


@pytest.mark.asyncio
async def test_agent_learns_and_applies_lessons_next_time(event_store):
    llm = RecordingLLM()
    runtime = AgentRuntime(event_store, llm=llm, reflect=True)
    runtime.active_agents["QuantDev"] = _config()

    first = await runtime.step("QuantDev", "Review the backtest", "task-1")
    assert len(first["lessons_learned"]) == 2
    assert runtime.memory.count("QuantDev") == 2

    learned_events = [e for e in event_store.get_events() if e["type"] == "LEARNED"]
    assert len(learned_events) == 1

    second = await runtime.step("QuantDev", "Review the backtest again", "task-2")
    assert second["lessons_applied"] == 2

    # The lessons must actually reach the prompt on the next task
    think_calls = [c for c in llm.calls if "Reflect: what should you check" not in " ".join(
        str(m.get("content") or "") for m in c["messages"]
    )]
    assert "Verify data freshness" in think_calls[1]["system"]
    assert "advisory notes only" in think_calls[1]["system"]


@pytest.mark.asyncio
async def test_no_learning_from_llm_failure(event_store):
    runtime = AgentRuntime(event_store, llm=FailingLLM(), reflect=True)
    runtime.active_agents["QuantDev"] = _config()

    result = await runtime.step("QuantDev", "Anything", "task-x")
    assert result["deliverable"].startswith("[LLM_UNAVAILABLE]")
    assert runtime.memory.count("QuantDev") == 0
    assert not [e for e in event_store.get_events() if e["type"] == "LEARNED"]


@pytest.mark.asyncio
async def test_reflection_can_be_disabled(event_store):
    llm = RecordingLLM()
    runtime = AgentRuntime(event_store, llm=llm, reflect=False)
    runtime.active_agents["QuantDev"] = _config()

    result = await runtime.step("QuantDev", "Review", "task-off")
    assert result.get("lessons_learned", []) == []
    assert runtime.memory.count("QuantDev") == 0


def test_parse_lessons_accepts_only_explicit_lessons():
    text = (
        "- **Why**: this task matters\n"
        "- Candidate Strategy Hypothesis:\n"
        "1. Some numbered answer fragment\n"
        "LESSON: Always verify data freshness before any analysis.\n"
        "LESSON: Check concentration before adding exposure.\n"
        "LESSON: Third lesson must be dropped.\n"
    )
    lessons = AgentRuntime._parse_lessons(text)
    assert lessons == [
        "Always verify data freshness before any analysis.",
        "Check concentration before adding exposure.",
    ]


def test_parse_lessons_rejects_junk_and_none():
    assert AgentRuntime._parse_lessons("NONE") == []
    assert AgentRuntime._parse_lessons("LESSON: too short") == []
    assert AgentRuntime._parse_lessons("LESSON: lowercase start is not a lesson.") == []
    assert AgentRuntime._parse_lessons("Just some prose without the prefix.") == []


def test_ceo_hiring_flow(event_store):
    ceo = CEOManager(event_store)
    req_id = ceo.create_hiring_request(
        "RiskOfficer",
        "Need better drawdown monitoring",
        {"drawdown_freq": "high"},
        5000
    )

    assert ceo.hiring_requests[req_id].status == RequestStatus.PENDING
    ceo.approve_hire(req_id)
    assert ceo.hiring_requests[req_id].status == RequestStatus.APPROVED
