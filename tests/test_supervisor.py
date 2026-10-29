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
    assert mcp.env["USERPROFILE"] == mcp.env["HOME"]          # Windows: Path.home() needs USERPROFILE
    assert "--transport" in mcp.argv and "127.0.0.1" in mcp.argv
    assert kids["brain"].env["FIRM_MCP_URL"].startswith("http://127.0.0.1:")


def test_filtered_env_drops_prefixes():
    out = filtered_env({"CAPITAL_X": "1", "OTHER": "2", "FIRM_API_TOKEN": "t"}, ("CAPITAL_",))
    assert out == {"OTHER": "2", "FIRM_NO_DOTENV": "1", "PYTHONUNBUFFERED": "1"}


def test_notify_zone_only_when_configured_and_only_it_sees_notify_tokens(tmp_path):
    assert "notify" not in _plan(tmp_path, FIRM_MCP="0")
    kids = _plan(tmp_path, FIRM_MCP="0", NOTIFY_TELEGRAM_BOT_TOKEN="123:abc", NOTIFY_TELEGRAM_CHAT_ID="42",
                 HEALTHCHECK_PING_URL="https://hc.example/x")
    notify = kids["notify"].env
    assert notify["NOTIFY_TELEGRAM_BOT_TOKEN"] == "123:abc" and notify["HEALTHCHECK_PING_URL"]
    assert not any(k.startswith(("CAPITAL_", "CAP_", "ANTHROPIC_")) for k in notify) and "FIRM_API_TOKEN" not in notify
    for other in ("money", "brain", "api"):
        assert not any(k.startswith(("NOTIFY_", "HEALTHCHECK_")) for k in kids[other].env)


def test_wait_on_halt_returns_when_operator_resumes(tmp_path, monkeypatch):
    import asyncio
    import types
    from src.firm import cli
    halt = tmp_path / "firm.halt"
    halt.write_text("x")
    monkeypatch.setattr(cli, "HALT_FILE", halt)

    async def scenario():
        task = asyncio.create_task(cli._wait_while_halted(types.SimpleNamespace(dashboard=False)))
        await asyncio.sleep(0.2)
        assert not task.done()            # still halted: nothing starts
        halt.unlink()                     # python -m src.firm resume
        return await asyncio.wait_for(task, 10)

    assert asyncio.run(scenario()) is True


def test_sigterm_stops_children_in_order_and_marks_termination(tmp_path):
    import asyncio
    import os
    import signal
    import sys
    from src.firm.supervisor import Child

    sup = Supervisor("PAPER", False, "127.0.0.1", 8000, tmp_path / "halt", env=dict(ENV))
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    sup.plan = lambda: [Child("money", sleeper, {"PATH": "/usr/bin"}), Child("brain", sleeper, {"PATH": "/usr/bin"})]

    async def scenario():
        task = asyncio.create_task(sup.run())
        await asyncio.sleep(1.0)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 30)

    asyncio.run(scenario())
    # run() must only return once every zone has really stopped (else docker kills them mid-shutdown)
    assert sup.terminated and sup._stopped.is_set() and all(c.proc.returncode is not None for c in sup.children)
