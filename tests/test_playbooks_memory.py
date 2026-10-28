"""Playbooks (versioniert, Freigabe mit Diff) und Widerspruchserkennung im Gedaechtnis."""
import json

import pytest

from src.firm.agents.memory import LessonStore, contradicts, polarity
from src.firm.agents.playbooks import PlaybookStore
from src.firm.agents.runtime import AgentConfig, AgentRuntime
from src.firm.bus.event_store import EventStore
from src.firm.llm.base import LLMProvider, LLMResponse


@pytest.fixture
def env(tmp_path):
    seeds = tmp_path / "pb"
    seeds.mkdir()
    (seeds / "RiskOfficer.md").write_text("# Playbook\n- Check halts first.\n- Quote facts.")
    store = EventStore(str(tmp_path / "e.db"))
    return store, PlaybookStore(store.db_path, seeds, event_store=store)


def test_playbook_seed_proposal_diff_and_approval(env):
    store, pbs = env
    v1 = pbs.active("RiskOfficer")
    assert v1["version"] == 1 and "Check halts first" in v1["text"]
    prop = pbs.propose("RiskOfficer", "RiskOfficer", "two stop-outs had no stop attached",
                       add_rule="Verify every open position has a stop level before anything else.")
    assert prop["status"] == "PROPOSED" and "+- Verify every open position" in prop["diff"]
    assert pbs.active("RiskOfficer")["version"] == 1, "not active before approval"
    pbs.decide(prop["id"], approve=True)
    active = pbs.active("RiskOfficer")
    assert active["version"] == 2 and "Verify every open position" in active["text"]
    assert [r["status"] for r in pbs.history("RiskOfficer")] == ["ARCHIVED", "ACTIVE"]
    types = [e["type"] for e in store.get_events()]
    assert "PLAYBOOK_PROPOSED" in types and "PLAYBOOK_APPROVED" in types


def test_playbook_rejection_and_supersede(env):
    _, pbs = env
    a = pbs.propose("RiskOfficer", "RiskOfficer", "reason one is long enough", add_rule="Rule A is a concrete rule here.")
    b = pbs.propose("RiskOfficer", "RiskOfficer", "reason two is long enough", remove_rule="Quote facts")
    assert [p["id"] for p in pbs.pending()] == [b["id"]]
    pbs.decide(b["id"], approve=False)
    assert pbs.active("RiskOfficer")["version"] == 1
    with pytest.raises(ValueError):
        pbs.decide(a["id"], approve=True)


def test_lesson_polarity_and_contradiction():
    a = "GOLD orb entries during the London open were profitable in 6 of 8 trades."
    b = "GOLD orb entries during the London open lost money in 5 of 7 trades."
    c = "EURUSD spread above 2 pips was common in the Asian session."
    assert polarity(a) == 1 and polarity(b) == -1
    assert contradicts(a, b)
    assert not contradicts(a, c)
    assert polarity("GOLD orb entries were not profitable at all.") == -1


def test_contradicting_lessons_are_both_kept_and_marked(tmp_path):
    ls = LessonStore(str(tmp_path / "l.db"))
    first = ls.add("RiskOfficer", "GOLD orb entries during the London open were profitable in 6 of 8 trades.")
    second = ls.add("RiskOfficer", "GOLD orb entries during the London open lost money in 5 of 7 trades.")
    assert second["contradicts"] == [first["lesson_id"]]
    rows = {r["lesson_id"]: r for r in ls.recent("RiskOfficer")}
    assert len(rows) == 2
    assert rows[first["lesson_id"]]["contradicts"] == second["lesson_id"]
    assert ls.delete(second["lesson_id"])
    assert not ls.recent("RiskOfficer")[0]["contradicts"]


class OneShot(LLMProvider):
    provider_name, model = "stub", "stub"

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def generate(self, system_prompt, messages, max_tokens=None, temperature=None, json_mode=False):
        self.calls.append(system_prompt)
        return LLMResponse(content=json.dumps(self.answer), model="stub", provider="stub")

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_runtime_loads_playbook_and_marks_disputed_lessons(env):
    store, pbs = env
    llm = OneShot({"summary": "ok", "actions": []})
    rt = AgentRuntime(store, llm=llm, playbooks=pbs)
    rt.active_agents["RiskOfficer"] = AgentConfig(name="RiskOfficer", role="Risk", system_prompt="You watch risk.",
                                                  tool_allowlist=[])
    rt.memory.add("RiskOfficer", "GOLD orb entries during the London open were profitable in 6 of 8 trades.")
    rt.memory.add("RiskOfficer", "GOLD orb entries during the London open lost money in 5 of 7 trades.")
    await rt.step("RiskOfficer", "Review GOLD orb London open", "t1", facts={})
    assert "YOUR PLAYBOOK v1" in llm.calls[0] and "Check halts first" in llm.calls[0]
    assert "[DISPUTED" in llm.calls[0]
