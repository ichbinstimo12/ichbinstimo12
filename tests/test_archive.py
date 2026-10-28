import json
import gzip

from src.firm.archive.store import Archive


def _trade(archive: Archive, kind: str, deal_id: str, **extra):
    payload = {
        "epic": "BTCUSD",
        "side": "SELL",
        "size": 0.01,
        "price": 80000.0,
        "deal_id": deal_id,
    }
    payload.update(extra)
    return archive.record_trade(kind, payload)


def test_archive_files_a_trade_per_day_and_in_the_ledger(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))

    path = _trade(archive, "FILLED", "deal-1")

    assert path is not None and path.exists()
    assert len(path.parent.name) == 10 and path.parent.name[4] == "-"
    assert (archive.trades_dir / "ledger.jsonl").exists()

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["kind"] == "FILLED"
    assert record["epic"] == "BTCUSD"
    assert record["side"] == "SELL"
    assert record["deal_id"] == "deal-1"


def test_archive_pairs_a_fill_with_its_real_close(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    _trade(archive, "FILLED", "deal-1")
    _trade(archive, "CLOSED", "deal-1", price=80123.5, profit=2.34, profit_currency="CHF")

    trades = archive.trades()

    assert len(trades) == 1
    row = trades[0]
    assert row["status"] == "closed"
    assert row["entry"] == 80000.0
    assert row["exit"] == 80123.5
    assert row["profit"] == 2.34
    assert row["profit_currency"] == "CHF"


def test_archive_never_invents_a_close_or_a_profit(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    _trade(archive, "FILLED", "deal-open")

    row = archive.trades()[0]

    assert row["status"] == "open"
    assert row["exit"] is None
    assert row["profit"] is None


def test_archive_pairs_a_close_that_carries_the_position_id(tmp_path):
    """Capital.com closes use the position id, which differs from the fill's deal id."""
    archive = Archive(str(tmp_path / "archiv"))
    _trade(archive, "FILLED", "deal-fill")
    _trade(archive, "CLOSED", "deal-position", price=80123.5, profit=2.34, profit_currency="CHF")

    row = archive.trades()[0]

    assert row["status"] == "closed"
    assert row["exit"] == 80123.5
    assert row["profit"] == 2.34


def test_archive_refuses_to_pair_an_ambiguous_close(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    _trade(archive, "FILLED", "deal-a")
    _trade(archive, "FILLED", "deal-b")
    _trade(archive, "CLOSED", "deal-position", price=80123.5, profit=2.34, profit_currency="CHF")

    assert [t["status"] for t in archive.trades()] == ["open", "open"]


def test_archive_files_tasks_and_actions_per_agent(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    task = {
        "task_id": "task-123",
        "title": "Review risk",
        "description": "Check the stop distances",
        "status": "DONE",
    }
    actions = [
        {"type": "THOUGHT", "ts": "2026-10-24T10:00:00", "payload_json": json.dumps({"content": "hmm"})},
    ]

    path = archive.record_task(
        "RiskOfficer",
        task,
        result={"deliverable": "stops are fine", "lessons_learned": ["keep stops wide"]},
        actions=actions,
    )

    assert path is not None and path.exists()
    assert path.parent == archive.agent_dir("RiskOfficer") / "tasks"

    rows = archive.agent_tasks("RiskOfficer")
    assert len(rows) == 1
    assert rows[0]["task_id"] == "task-123"
    assert rows[0]["result"]["deliverable"] == "stops are fine"
    assert rows[0]["actions"][0]["type"] == "THOUGHT"

    actions_log = (archive.agent_dir("RiskOfficer") / "actions.jsonl").read_text(encoding="utf-8")
    assert "keep stops wide" in actions_log


def test_archive_context_gives_the_agent_its_own_history(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    archive.record_task(
        "CEO",
        {"task_id": "t1", "title": "Set direction", "status": "DONE"},
        result={"deliverable": "focus on BTCUSD", "lessons_learned": ["one market is enough"]},
    )
    _trade(archive, "FILLED", "d1")
    _trade(archive, "CLOSED", "d1", price=80100.0, profit=1.5, profit_currency="CHF")

    block = archive.context_for("CEO")

    assert "Your archive folder" not in block
    assert "Set direction" in block
    assert "one market is enough" in block
    assert "SELL" in block and "BTCUSD" in block
    assert "+1.50 CHF" in block


def test_archive_context_is_honest_when_empty(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))

    block = archive.context_for("Newbie")

    assert "first task" in block
    assert "No trades have been archived yet" in block


def test_archive_stats_count_real_trades_only(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    _trade(archive, "FILLED", "d1")
    _trade(archive, "CLOSED", "d1", price=80100.0, profit=1.25, profit_currency="CHF")
    _trade(archive, "FILLED", "d2")
    archive.record_task("CEO", {"task_id": "t1", "title": "x"}, result={})

    stats = archive.stats()

    assert stats["trades_total"] == 2
    assert stats["trades_closed"] == 1
    assert stats["trades_with_profit"] == 1
    assert stats["realized_profit"] == 1.25
    assert stats["tasks_per_agent"]["CEO"] == 1


def test_archive_survives_an_unwritable_root(tmp_path, monkeypatch):
    """A broken disk must never raise into the trading path."""
    archive = Archive(str(tmp_path / "archiv"))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", boom)

    assert archive.record_trade("FILLED", {"epic": "BTCUSD", "deal_id": "x"}) is None


class _FakeStore:
    def __init__(self, events):
        self._events = events

    def get_events(self, limit=5000):
        return self._events[:limit]


def _event(ts, type_, payload):
    return {"ts": ts, "type": type_, "payload_json": json.dumps(payload)}


def test_backfill_imports_pre_archive_trades(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    store = _FakeStore([
        _event("2026-10-24T14:26:15", "ORDER_FILLED",
               {"order_id": "d1", "epic": "BTCUSD", "side": "SELL", "size": 0.0269, "price": 79975.75}),
        _event("2026-10-24T14:38:37", "POSITION_CLOSED",
               {"epic": "BTCUSD", "direction": "SELL", "size": 0.0269, "reason": "closed at broker"}),
    ])

    written = archive.backfill_from_events(store)

    assert written == 2
    trades = archive.trades()
    assert len(trades) == 1
    assert trades[0]["status"] == "closed"
    assert trades[0]["entry"] == 79975.75
    # the broker confirmation was never captured, so there is no exit or P/L
    assert trades[0]["exit"] is None
    assert trades[0]["profit"] is None
    assert trades[0]["time"].startswith("2026-10-24T14:26")


def test_backfill_is_idempotent(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    events = [
        _event("2026-10-24T14:26:15", "ORDER_FILLED",
               {"order_id": "d1", "epic": "BTCUSD", "side": "SELL", "size": 0.0269, "price": 79975.75}),
        _event("2026-10-24T14:38:37", "POSITION_CLOSED",
               {"epic": "BTCUSD", "direction": "SELL", "size": 0.0269}),
    ]
    store = _FakeStore(events)

    archive.backfill_from_events(store)
    assert archive.backfill_from_events(store) == 0
    assert len(archive.trade_events(limit=0)) == 2


def test_backfill_skips_an_ambiguous_close(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    store = _FakeStore([
        _event("2026-10-24T10:00:00", "ORDER_FILLED",
               {"order_id": "d1", "epic": "BTCUSD", "side": "SELL", "size": 0.01, "price": 80000.0}),
        _event("2026-10-24T10:05:00", "ORDER_FILLED",
               {"order_id": "d2", "epic": "BTCUSD", "side": "SELL", "size": 0.01, "price": 80100.0}),
        _event("2026-10-24T10:10:00", "POSITION_CLOSED",
               {"epic": "BTCUSD", "direction": "SELL", "size": 0.01}),
    ])

    archive.backfill_from_events(store)

    # two identical open fills -> we refuse to guess which one closed
    assert [t["status"] for t in archive.trades()] == ["open", "open"]
    assert len(archive.trade_events(limit=0)) == 2


def test_gzip_append_is_readable_and_incremental(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    target = archive.compressed_dir / "2026-09.jsonl.gz"

    archive.append_gzip_jsonl(target, [{"a": 1}, {"a": 2}])
    archive.append_gzip_jsonl(target, [{"a": 3}])

    with gzip.open(target, "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]

    assert [r["a"] for r in rows] == [1, 2, 3]
    assert len(archive.trade_events()) == 3
