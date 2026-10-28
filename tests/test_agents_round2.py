"""Token-Budgets, messbare Agenten-Qualitaet, relevante Lektionen, Tagesbericht, Einstellung nur mit Freigabe."""
import json

import pytest

from src.firm.agents.ceo_manager import CEOManager
from src.firm.agents.memory import LessonStore
from src.firm.agents.metrics import agent_metrics, tokens_today
from src.firm.agents.runtime import AgentConfig, AgentRuntime
from src.firm.bus.event_store import EventStore
from src.firm.llm.base import LLMProvider, LLMResponse
from src.firm.reports.daily import DailyReporter


class CountingLLM(LLMProvider):
    provider_name = "stub"
    model = "stub"

    def __init__(self, answer, tokens=500):
        self.answer, self.tokens, self.calls = answer, tokens, 0

    async def generate(self, system_prompt, messages, max_tokens=None, temperature=None, json_mode=False):
        self.calls += 1
        return LLMResponse(content=json.dumps(self.answer), model="stub", provider="stub",
                           usage={"input_tokens": self.tokens, "output_tokens": 0})

    async def close(self):
        pass


@pytest.fixture
def store(tmp_path):
    return EventStore(str(tmp_path / "a.db"))


def _cfg(budget=1000):
    return AgentConfig(name="RiskOfficer", role="Risk", system_prompt="x", tool_allowlist=[], daily_token_budget=budget)


@pytest.mark.asyncio
async def test_daily_token_budget_is_enforced(store):
    llm = CountingLLM({"summary": "ok", "actions": []}, tokens=600)
    rt = AgentRuntime(store, llm=llm)
    rt.active_agents["RiskOfficer"] = _cfg(budget=1000)
    assert (await rt.step("RiskOfficer", "t", "1", facts={}))["tokens"] == 600
    await rt.step("RiskOfficer", "t", "2", facts={})
    third = await rt.step("RiskOfficer", "t", "3", facts={})
    assert third["status"] == "budget_exhausted" and llm.calls == 2
    assert tokens_today(store, "RiskOfficer") == 1200


@pytest.mark.asyncio
async def test_firm_wide_token_budget(store):
    llm = CountingLLM({"summary": "ok", "actions": []}, tokens=600)
    rt = AgentRuntime(store, llm=llm, firm_token_budget=500)
    rt.active_agents["RiskOfficer"] = _cfg(budget=10_000)
    await rt.step("RiskOfficer", "t", "1", facts={})
    assert (await rt.step("RiskOfficer", "t", "2", facts={}))["status"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_metrics_measure_quality_and_feed_back_into_the_prompt(store):
    good = CountingLLM({"summary": "ok", "claims": {"open_positions": 0}, "actions": []})
    rt = AgentRuntime(store, llm=good)
    rt.active_agents["RiskOfficer"] = _cfg(budget=10**6)
    facts = {"open_positions": 1, "account": {}}
    await rt.step("RiskOfficer", "t", "1", facts=facts)          # contradicts facts -> invalid
    good.answer = {"summary": "one position open", "claims": {"open_positions": 1}, "actions": []}
    await rt.step("RiskOfficer", "t", "2", facts=facts)
    m = agent_metrics(store)["RiskOfficer"]
    assert m["tasks"] == 2 and m["invalid"] == 1 and m["valid_rate"] == 0.5
    assert "YOUR TRACK RECORD" in rt._user_prompt(rt.active_agents["RiskOfficer"], "t", "3", facts)


def test_relevant_lessons_beat_merely_recent_ones(tmp_path):
    ls = LessonStore(str(tmp_path / "l.db"))
    ls.add("A", "GOLD donchian_trend stops during London open were hit 4 times out of 5.", confidence=0.5)
    ls.add("A", "EURUSD spread above 2 pips blocked 6 entries during the Asian session.", confidence=0.6)
    top = ls.relevant("A", "Review GOLD donchian_trend losses", limit=1)
    assert "GOLD" in top[0]["lesson"]


def test_applying_a_lesson_does_not_extend_its_life(tmp_path):
    ls = LessonStore(str(tmp_path / "l.db"))
    row = ls.add("A", "GOLD donchian_trend stops during London open were hit often.")
    ls.record_applied([row["lesson_id"]])
    assert ls.recent("A")[0]["expires_at"] == row["expires_at"]


def test_daily_report_uses_real_numbers_only(store, tmp_path):
    store.append("SYSTEM", "ACCOUNT_SNAPSHOT", {"equity": 1000.0})
    store.append("SYSTEM", "ACCOUNT_SNAPSHOT", {"equity": 990.0})
    store.append("TRADING_DESK", "TRADE_RESULT", {"pnl": -10.0, "r": -1.0})
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).date().isoformat()
    rep = DailyReporter(store, tmp_path)
    r = rep.generate_report(day)
    assert r["equity_change"] == -10.0 and r["closed_trades"] == 1 and r["expectancy_r"] == -1.0
    assert r["max_intraday_drawdown_pct"] == pytest.approx(1.0)
    path = rep.save_to_disk(r)
    assert "Tagesbericht" in path.read_text()
    empty = rep.generate_report("2000-01-01")
    assert empty["equity_start"] is None and "—" in rep.to_markdown(empty)


def test_hiring_needs_operator_approval(store):
    hired = []
    ceo = CEOManager(store, on_approved=hired.append)
    rid = ceo.create_hiring_request("DataEngineer", "CEO: backlog of 12 data checks", {}, 40000)
    assert hired == []
    ceo.approve_hire(rid)
    assert hired == ["DataEngineer"]


def test_propose_hire_action_validation():
    from src.firm.agents.actions import validate_action
    ok, norm = validate_action({"type": "propose_hire", "role": "DataEngineer", "reason": "12 open data-quality tasks"},
                               ["propose_hire"], [])
    assert ok and norm["role"] == "DataEngineer"
    ok, _ = validate_action({"type": "propose_hire", "role": "../../etc", "reason": "12 open data-quality tasks"},
                            ["propose_hire"], [])
    assert not ok
