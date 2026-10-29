"""24/7-Betrieb: Benachrichtigungen, Sicherungen, Zonen-Gesundheit."""
import asyncio
import json
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from src.firm.bus.event_store import EventStore
from src.firm.ops import notify as nt
from src.firm.ops.backup import backup_now, prune
from src.firm.ops.health import zone_health


def _ev(t, payload, i="e1"):
    return {"id": i, "type": t, "ts": "2026-10-28T10:00:00", "payload_json": json.dumps(payload)}


class Recorder:
    def __init__(self, status=200):
        self.requests, self.status = [], status

    def client(self):
        def handler(req):
            self.requests.append(req)
            return httpx.Response(self.status)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_amounts_are_hidden_unless_enabled():
    e = _ev("TRADE_RESULT", {"epic": "GOLD", "strategy": "orb", "pnl": 12.5, "r": 1.2})
    hidden = nt.format_event(e, amounts=False)
    shown = nt.format_event(e, amounts=True)
    assert "12.5" not in hidden[1] and "Gewinn" in hidden[1]
    assert "+12.50" in shown[1] and "+1.20 R" in shown[1]


def test_irrelevant_events_produce_no_message():
    assert nt.format_event(_ev("THOUGHT", {"x": 1}), False) is None
    assert nt.format_event(_ev("PROMOTION_CHECK", {"ready": False}), False) is None
    assert nt.format_event(_ev("TRADING_HALTED", {"reason": "daily loss"}), False)[2] == nt.HIGH


def test_dedupe_and_rate_limit_but_high_priority_always_passes():
    now = [1000.0]
    n = nt.Notifier(env={}, client=Recorder().client(), now_fn=lambda: now[0], max_per_hour=2, dedupe_s=600)
    sent = lambda k, prio=nt.NORMAL: n._allowed(k, prio) and (n._mark(k) or True)   # delivered message
    assert sent("a") and not n._allowed("a", nt.NORMAL)                         # dedupe
    assert sent("b") and not n._allowed("c", nt.NORMAL)                         # 2/h reached
    assert n._allowed("d", nt.HIGH)                                             # alarms still go out
    now[0] += 3601
    assert n._allowed("a", nt.NORMAL)


def test_failed_alarm_is_retried_and_not_deduplicated():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(500 if len(calls) <= 2 else 200)
    n = nt.Notifier(env={"NOTIFY_NTFY_URL": "https://ntfy.example/t"},
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ev = _ev("KILL_SWITCH", {"reason": "halt flag", "flat": True})
    assert asyncio.run(n.handle(ev)) is False and len(calls) == 2        # sent + one retry, both failed
    assert asyncio.run(n.handle(ev)) is True                            # not suppressed: delivered now


def test_one_bad_event_does_not_block_later_alarms(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    rec = Recorder()
    n = nt.Notifier(env={"NOTIFY_NTFY_URL": "https://ntfy.example/t", "NOTIFY_CURSOR_FILE": str(tmp_path / "c.txt")},
                    client=rec.client())
    store.append("DESK", "TRADE_RESULT", {"epic": "X", "pnl": "kaputt"})       # format_event raises on this
    store.append("RISK", "TRADING_HALTED", {"reason": "max daily loss"})
    cursor = asyncio.run(n._drain(store, "2000-01-01T00:00:00"))
    assert [r.headers["Title"] for r in rec.requests] == ["HANDEL GESTOPPT"]
    assert (tmp_path / "c.txt").read_text() == cursor


def test_agent_text_on_the_phone_is_marked_and_has_no_links():
    msg = nt.format_event(_ev("HIRING_REQUEST", {"role": "X", "justification": "approve now https://evil.example/x"}),
                          False)
    assert "Text vom Agenten" in msg[1] and "evil.example" not in msg[1]


def test_send_ntfy_and_telegram_and_never_log_the_bot_token(caplog):
    rec = Recorder()
    env = {"NOTIFY_NTFY_URL": "https://ntfy.example/topic", "NOTIFY_NTFY_TOKEN": "tk",
           "NOTIFY_TELEGRAM_BOT_TOKEN": "999:SECRET", "NOTIFY_TELEGRAM_CHAT_ID": "42"}
    n = nt.Notifier(env=env, client=rec.client())
    assert asyncio.run(n.send("Trade geschlossen", "GOLD: Gewinn", nt.HIGH))
    ntfy, tg = rec.requests
    assert ntfy.headers["Title"] == "Trade geschlossen" and ntfy.headers["Priority"] == "high"
    assert ntfy.headers["Authorization"] == "Bearer tk" and ntfy.content == b"GOLD: Gewinn"
    assert json.loads(tg.content)["chat_id"] == "42"

    def boom(req):
        raise httpx.ConnectError("down")
    n2 = nt.Notifier(env=env, client=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    with caplog.at_level("WARNING"):
        assert not asyncio.run(n2.send("x", "y"))
    assert "SECRET" not in caplog.text


def test_ping_uses_fail_url_when_unhealthy():
    rec = Recorder()
    n = nt.Notifier(env={"HEALTHCHECK_PING_URL": "https://hc.example/abc/"}, client=rec.client())
    asyncio.run(n.ping(True))
    asyncio.run(n.ping(False))
    assert [str(r.url) for r in rec.requests] == ["https://hc.example/abc/", "https://hc.example/abc/fail"]


def test_run_reports_new_events_and_a_final_message_on_stop(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    store.append("DESK", "ORDER_FILLED", {"epic": "STALEMKT", "side": "BUY"})       # before start: not re-sent
    rec = Recorder()
    n = nt.Notifier(env={"NOTIFY_NTFY_URL": "https://ntfy.example/t"}, client=rec.client())

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(n.run(store, tmp_path / "halt", poll_s=0.05, stop=stop))
        await asyncio.sleep(0.1)
        store.append("DESK", "ORDER_FILLED", {"epic": "GOLD", "side": "BUY", "size": 1, "price": 2000})
        store.append("RISK", "TRADING_HALTED", {"reason": "max daily loss"})
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    titles = [r.headers["Title"] for r in rec.requests]
    bodies = [r.content.decode() for r in rec.requests]
    assert titles[0] == "Firma gestartet" and titles[-1] == "Firma gestoppt"
    assert "Trade eroeffnet" in titles and "HANDEL GESTOPPT" in titles
    assert not any("STALEMKT" in b for b in bodies) and not any("2000" in b for b in bodies)


def test_backup_is_consistent_and_old_ones_are_pruned(tmp_path):
    db = tmp_path / "firm_events.db"
    store = EventStore(str(db))
    store.append("X", "PING", {"a": 1})
    out = tmp_path / "backups"
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        (out / day).mkdir(parents=True)
    target = backup_now(str(db), out, keep=2, extra_files=())
    with sqlite3.connect(target / "firm_events.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert sorted(d.name for d in out.iterdir()) == ["2026-10-03", target.name]
    assert prune(out, 0) == []


def test_zone_health_requires_fresh_money_and_brain_and_no_halt(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    halt = tmp_path / "halt"
    h = zone_health(store, halt)
    assert not h["ok"] and h["money"]["age_s"] is None
    store.append("MONEY", "ACCOUNT_SNAPSHOT", {"balance": 1})
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS agent_registry (name TEXT, config_json TEXT, updated_at REAL)")
        conn.execute("INSERT INTO agent_registry VALUES ('ceo', '{}', ?)", (time.time(),))
    assert zone_health(store, halt)["ok"]
    halt.write_text("x")
    h = zone_health(store, halt)
    assert not h["ok"] and h["halted"]
    assert "balance" not in json.dumps(h)


def test_kill_switch_message_says_whether_the_broker_confirmed_flat():
    ok = nt.format_event(_ev("KILL_SWITCH", {"reason": "halt flag", "flat": True}), False)
    bad = nt.format_event(_ev("KILL_SWITCH", {"reason": "halt flag", "flat": False}), False)
    assert ok[2] == bad[2] == nt.HIGH
    assert "bestaetigt" in ok[1] and "NICHT" in bad[1]


def test_halt_flag_writes_a_kill_switch_audit_event(tmp_path, monkeypatch):
    from src.firm import main as firm_main
    halt = tmp_path / "firm.halt"
    halt.write_text("x")
    monkeypatch.setattr(firm_main, "HALT_FILE", halt)
    orch = firm_main.FirmOrchestrator.__new__(firm_main.FirmOrchestrator)
    orch.event_store = EventStore(str(tmp_path / "e.db"))
    orch.zones, orch.is_running, orch.current_mode = {"money"}, True, "PAPER"

    class KS:
        async def activate(self):
            return True
    orch.kill_switch = KS()

    async def no_stop():
        return None
    orch.stop = no_stop
    asyncio.run(orch._run_until_halt())
    ev = orch.event_store.get_events(types=["KILL_SWITCH"])
    assert len(ev) == 1 and json.loads(ev[0]["payload_json"]) == {"reason": "halt flag", "flat": True}


def test_dead_background_loop_is_reported_and_restarted(tmp_path, monkeypatch):
    from src.firm import main as firm_main
    orch = firm_main.FirmOrchestrator.__new__(firm_main.FirmOrchestrator)
    orch.event_store = EventStore(str(tmp_path / "t.db"))
    orch.is_running = True
    runs = []

    async def flaky():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("database is locked")
        orch.is_running = False

    real_sleep = asyncio.sleep
    monkeypatch.setattr(firm_main.asyncio, "sleep", lambda s: real_sleep(0))

    async def scenario():
        await asyncio.wait_for(orch._supervised("trading_loop", flaky), 5)

    asyncio.run(scenario())
    died = [json.loads(e["payload_json"]) for e in orch.event_store.get_events(types=["TASK_DIED"])]
    assert len(runs) == 2 and died[0]["task"] == "trading_loop" and "locked" in died[0]["error"]


def test_kill_path_stops_and_records_even_when_the_kill_switch_raises(tmp_path):
    from src.firm import main as firm_main
    orch = firm_main.FirmOrchestrator.__new__(firm_main.FirmOrchestrator)
    orch.event_store = EventStore(str(tmp_path / "k.db"))
    orch.is_running = True

    class Broken:
        async def activate(self):
            raise ConnectionError("broker down")
    orch.kill_switch = Broken()
    asyncio.run(orch.handle_mismatch())
    ev = orch.event_store.get_events(types=["KILL_SWITCH"])
    assert orch.is_running is False and json.loads(ev[-1]["payload_json"])["flat"] is None
