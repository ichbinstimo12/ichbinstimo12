"""Eval-Harness (nach ECC eval-harness): Szenarien laden, deterministisch bewerten, pass@k."""
import json

import pytest

from src.firm.agents.evals import grade, load_scenarios, run_all, run_scenario
from src.firm.llm.base import LLMProvider, LLMResponse


class Fixed(LLMProvider):
    provider_name, model = "stub", "stub-1"

    def __init__(self, answer):
        self.answer = answer

    async def generate(self, system_prompt, messages, max_tokens=None, temperature=None, json_mode=False):
        return LLMResponse(content=json.dumps(self.answer), model="stub", provider="stub")

    async def close(self):
        pass


def test_all_shipped_scenarios_load_and_name_existing_roles():
    from pathlib import Path
    scs = load_scenarios()
    assert len(scs) >= 6
    for sc in scs:
        assert (Path("config/roles") / sc["role_file"]).exists(), sc["name"]
        assert sc.get("checks")


@pytest.mark.asyncio
async def test_good_answer_passes_and_contradiction_fails():
    sc = next(s for s in load_scenarios() if s["name"] == "risk_open_position_claims")
    good = await run_scenario(sc, Fixed({"summary": "GOLD long open with stop.", "claims": {"open_positions": 1},
                                         "actions": []}), runs=2)
    assert good["pass_all_k"] is True
    bad = await run_scenario(sc, Fixed({"summary": "No positions.", "claims": {"open_positions": 0}, "actions": []}), runs=2)
    assert bad["pass_at_k"] is False and "valid" in bad["outcomes"][0]["fails"][0]


@pytest.mark.asyncio
async def test_required_action_is_graded():
    sc = next(s for s in load_scenarios() if s["name"] == "redteam_losing_streak")
    lazy = await run_scenario(sc, Fixed({"summary": "Looks fine.", "actions": []}), runs=1)
    assert not lazy["pass_at_k"]
    strict = await run_scenario(sc, Fixed({"summary": "6 losses in a row on GOLD orb", "actions": [
        {"type": "tighten_risk", "epic": "GOLD", "factor": 0.0, "hours": 48, "reason": "6 consecutive losses, kill criterion"}]}), runs=1)
    assert strict["pass_at_k"]


@pytest.mark.asyncio
async def test_report_is_written(tmp_path):
    report = await run_all(Fixed({"summary": "research pending, no verdicts yet", "actions": []}), runs=1,
                           only="ceo_no_verdict", out_dir=tmp_path)
    assert report["scenarios"][0]["pass_at_k"] and (tmp_path / report["path"].split("/")[-1]).exists()


def test_grade_mentions_none():
    assert grade({"checks": [{"type": "mentions_none", "words": ["garantiert"]}]},
                 {"status": "success", "deliverable": "Gewinn garantiert", "actions": []})
