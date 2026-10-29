import pytest
from fastapi.testclient import TestClient

import src.firm.api.main as api_module
from src.firm.agents.task_queue import TaskQueue
from src.firm.api.main import create_app
from src.firm.archive.store import Archive
from src.firm.bus.event_store import EventStore

TOKEN = "test-secret-token"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "HALT_FILE", tmp_path / "firm.halt")
    store = EventStore(str(tmp_path / "api.db"))
    queue = TaskQueue(store.db_path, store)
    app = create_app(
        event_store=store,
        task_queue=queue,
        api_token=TOKEN,
        allowed_origins=["http://127.0.0.1:8000"],
    )
    return TestClient(app), store, queue


def test_health_is_open(api):
    client, _, _ = api
    assert client.get("/health").json() == {"status": "ok"}


def test_status_requires_token(api):
    client, _, _ = api
    assert client.get("/api/status").status_code == 401

    response = client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ONLINE"
    assert "tasks" in body
    assert body["mode"] in ("PAPER", "BACKTEST", "LIVE_MICRO", "LIVE")


def test_events_and_tasks_endpoints(api):
    client, store, queue = api
    store.append("CEO", "TEST_EVENT", {"msg": "hello"})
    queue.create_task("CEO", "QuantDeveloper", "Task", "Do it")

    headers = {"Authorization": f"Bearer {TOKEN}"}
    events = client.get("/api/events?limit=10", headers=headers)
    assert events.status_code == 200
    assert len(events.json()["events"]) >= 1

    tasks = client.get("/api/tasks", headers=headers)
    assert tasks.status_code == 200
    assert len(tasks.json()["tasks"]) == 1


def test_halt_writes_flag(api):
    client, _, _ = api
    response = client.post("/api/halt", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json()["status"] == "HALTED"
    assert (api_module.HALT_FILE).exists()


def test_archive_endpoints_without_orchestrator(api, tmp_path, monkeypatch):
    """Split mode: the API process reads the archive from disk itself (no orchestrator in this process)."""
    monkeypatch.setenv("FIRM_ARCHIVE_DIR", str(tmp_path / "archiv"))
    client, _, _ = api
    headers = {"Authorization": f"Bearer {TOKEN}"}

    body = client.get("/api/archive", headers=headers).json()
    assert body["available"] is True
    assert body["trades"] == []

    trades = client.get("/api/archive/trades", headers=headers).json()
    assert trades == {"trades": []}

    agent = client.get("/api/archive/agents/CEO", headers=headers).json()
    assert agent["agent"] == "CEO"
    assert agent["tasks"] == []


def test_archive_endpoints_read_the_filing_cabinet(tmp_path, monkeypatch):
    monkeypatch.setattr(api_module, "HALT_FILE", tmp_path / "firm.halt")
    store = EventStore(str(tmp_path / "api.db"))
    queue = TaskQueue(store.db_path, store)
    archive = Archive(str(tmp_path / "archiv"))
    archive.record_trade("FILLED", {"epic": "BTCUSD", "side": "SELL", "size": 0.01, "price": 80000.0, "deal_id": "d1"})
    archive.record_trade("CLOSED", {"epic": "BTCUSD", "side": "SELL", "size": 0.01, "price": 80100.0,
                                    "deal_id": "d1", "profit": 1.5, "profit_currency": "CHF"})
    archive.record_task("CEO", {"task_id": "t1", "title": "Set direction", "status": "DONE"},
                        result={"deliverable": "focus on BTCUSD", "lessons_learned": ["one market is enough"]})

    class StubOrchestrator:
        pass

    orchestrator = StubOrchestrator()
    orchestrator.archive = archive
    app = create_app(
        event_store=store,
        task_queue=queue,
        orchestrator=orchestrator,
        api_token=TOKEN,
        allowed_origins=["http://127.0.0.1:8000"],
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    body = client.get("/api/archive", headers=headers).json()
    assert body["available"] is True
    assert body["stats"]["trades_total"] == 1
    assert body["stats"]["trades_closed"] == 1
    assert body["stats"]["realized_profit"] == 1.5
    assert body["trades"][0]["epic"] == "BTCUSD"
    assert body["trades"][0]["status"] == "closed"
    assert body["trades"][0]["profit"] == 1.5

    agent = client.get("/api/archive/agents/CEO", headers=headers).json()
    assert agent["tasks"][0]["task_id"] == "t1"
    assert agent["tasks"][0]["result"]["lessons_learned"] == ["one market is enough"]

    status = client.get("/api/status", headers=headers).json()
    assert status["archive"]["trades_total"] == 1


def test_websocket_requires_valid_token(api):
    client, _, _ = api
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/office?token=wrong") as ws:
            ws.receive_json()

    with client.websocket_connect(f"/ws/office?token={TOKEN}") as ws:
        first = ws.receive_json()
        assert first["type"] == "SYNC"
        assert "data" in first
        assert "status" in first


def test_deep_health_is_open_but_reports_503_until_zones_are_fresh(api):
    client, store, _ = api
    r = client.get("/health/deep")
    assert r.status_code == 503 and r.json()["money"]["ok"] is False
    assert "balance" not in r.text


def test_websocket_rejects_foreign_origin(api):
    client, _, _ = api
    with client.websocket_connect(f"/ws/office?token={TOKEN}", headers={"origin": "https://evil.example"}) as ws:
        with pytest.raises(Exception):
            ws.receive_json()
    with client.websocket_connect(f"/ws/office?token={TOKEN}", headers={"origin": "http://127.0.0.1:8000"}) as ws:
        assert ws.receive_json()["type"]


def test_memedesk_endpoints_report_kill_and_refuse_live(api, tmp_path, monkeypatch):
    import json as _json
    client, store, _ = api
    monkeypatch.setattr(api_module, "MEME_DIR", tmp_path / "md")
    monkeypatch.setattr(api_module, "MEME_JOURNAL", tmp_path / "j.jsonl")
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/memedesk").status_code == 401
    body = client.get("/api/memedesk", headers=h).json()
    assert body["running"] is False and body["live_gate"]["trades"] == 0
    assert client.post("/api/memedesk/kill", headers=h).json()["status"] == "KILL_REQUESTED"
    assert (tmp_path / "md" / "halt").exists()
    r = client.post("/api/memedesk/mode", headers=h, json={"mode": "LIVE"})
    assert r.status_code == 409 and r.json()["granted"] is False and "100 Trades" in r.json()["reason"]
    assert _json.loads(store.get_events(types=["MEME_MODE_REQUEST"])[-1]["payload_json"])["granted"] is False
