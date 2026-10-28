import pytest

from src.firm.agents.runtime import AgentRuntime
from src.firm.bus.event_store import EventStore
from src.firm.llm.base import LLMProvider, LLMResponse


class StubLLM(LLMProvider):
    provider_name = "stub"

    async def generate(self, system_prompt, messages, max_tokens=None, temperature=None):
        return LLMResponse(content="stub thought", model="stub", provider="stub")

    async def close(self):
        pass


def test_role_configs_are_loaded(tmp_path):
    store = EventStore(str(tmp_path / "roles.db"))
    runtime = AgentRuntime(store, llm=StubLLM())

    loaded = runtime.load_agents_from_dir("config/roles", only=["CEO", "RiskOfficer"])
    assert loaded == ["CEO", "RiskOfficer"]
    assert runtime.active_agents["CEO"].reporting_line is None
    assert runtime.active_agents["RiskOfficer"].reporting_line == "CEO"
    assert runtime.active_agents["RiskOfficer"].name == "RiskOfficer"


def test_load_agent_accepts_yaml(tmp_path):
    role_file = tmp_path / "test_role.yaml"
    role_file.write_text(
        "name: TestAgent\n"
        "role: Tester\n"
        "system_prompt: Test prompt\n"
        "tool_allowlist: [read_events]\n"
        "model: llama3.1:8b\n"
        "daily_token_budget: 1000\n"
        "reporting_line: null\n",
        encoding="utf-8",
    )

    store = EventStore(str(tmp_path / "roles2.db"))
    runtime = AgentRuntime(store, llm=StubLLM())
    config = runtime.load_agent(str(role_file))
    assert config.name == "TestAgent"
    assert config.tool_allowlist == ["read_events"]
    assert config.daily_token_budget == 1000
