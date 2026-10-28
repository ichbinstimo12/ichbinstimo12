"""Prozesstrennung: Welcher Prozess bekommt welche Schluessel."""
from pathlib import Path

from src.firm.supervisor import Supervisor, filtered_env

ENV = {"PATH": "/usr/bin", "HOME": "/tmp", "CAPITAL_API_KEY": "k", "CAPITAL_IDENTIFIER": "i",
       "CAPITAL_PASSWORD": "p", "ANTHROPIC_API_KEY": "sk-ant", "FIRM_API_TOKEN": "tok", "LLM_PROVIDER": "anthropic"}


def _plan(tmp_path, **extra):
    env = dict(ENV, **extra)
    return {c.name: c for c in Supervisor("PAPER", True, "127.0.0.1", 8000, tmp_path / "halt", env=env).plan()}


def test_brain_never_gets_broker_keys_and_cannot_reload_them(tmp_path):
    kids = _plan(tmp_path, FIRM_MCP="0")
    brain = kids["brain"].env
    assert not any(k.startswith(("CAPITAL_", "CAP_")) for k in brain)
    assert brain["ANTHROPIC_API_KEY"] == "sk-ant"
    assert brain["FIRM_NO_DOTENV"] == "1"
    assert brain["FIRM_MCP"] == "0"


def test_money_has_broker_keys_but_no_llm_key(tmp_path):
    money = _plan(tmp_path, FIRM_MCP="0")["money"]
    assert money.env["CAPITAL_API_KEY"] == "k" and "ANTHROPIC_API_KEY" not in money.env
    assert money.critical and money.argv[-3:] == ["money", "--mode", "PAPER"]


def test_api_gets_no_secrets_except_its_own_token(tmp_path):
    api = _plan(tmp_path, FIRM_MCP="0")["api"].env
    assert "CAPITAL_API_KEY" not in api and "ANTHROPIC_API_KEY" not in api
    assert api["FIRM_API_TOKEN"] == "tok"


def test_mcp_server_runs_with_demo_keys_and_trading_disabled(tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/" + cmd)
    kids = _plan(tmp_path)
    mcp = kids["mcp"]
    assert mcp.env["CAP_ALLOW_TRADING"] == "false" and mcp.env["CAP_DRY_RUN"] == "true"
    assert mcp.env["CAP_ENV"] == "demo" and "ANTHROPIC_API_KEY" not in mcp.env
    assert "--transport" in mcp.argv and "127.0.0.1" in mcp.argv
    assert kids["brain"].env["FIRM_MCP_URL"].startswith("http://127.0.0.1:")


def test_filtered_env_drops_prefixes():
    out = filtered_env({"CAPITAL_X": "1", "OTHER": "2", "FIRM_API_TOKEN": "t"}, ("CAPITAL_",))
    assert out == {"OTHER": "2", "FIRM_NO_DOTENV": "1", "PYTHONUNBUFFERED": "1"}
