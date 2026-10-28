import gzip
import json
from datetime import date, timedelta

from src.firm.archive.curator import ArchiveCurator
from src.firm.archive.store import Archive


def _task(archive: Archive, agent: str, task_id: str) -> None:
    archive.record_task(
        agent,
        {"task_id": task_id, "title": f"task {task_id}", "status": "DONE"},
        result={"deliverable": f"did {task_id}", "lessons_learned": [f"learned {task_id}"]},
    )


def _trade_event(recorded_at: str, kind: str, deal_id: str, **extra) -> dict:
    record = {
        "recorded_at": recorded_at,
        "kind": kind,
        "epic": "BTCUSD",
        "side": "SELL",
        "size": 0.01,
        "price": 80000.0,
        "deal_id": deal_id,
        "reason": None,
        "profit": None,
        "profit_currency": None,
        "source": "trading_desk",
        "raw": {},
    }
    record.update(extra)
    return record


def test_curator_keeps_only_the_latest_tasks_per_agent(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    for i in range(6):
        _task(archive, "CEO", f"t{i}")

    curator = ArchiveCurator(archive, keep_tasks=3, keep_trade_days=3)
    report = curator.run()

    tasks_dir = archive.agent_dir("CEO") / "tasks"
    kept = sorted(p.name for p in tasks_dir.glob("*.json"))

    assert len(kept) == 3
    kept_ids = sorted(
        json.loads((tasks_dir / name).read_text(encoding="utf-8"))["task_id"] for name in kept
    )
    assert kept_ids == ["t3", "t4", "t5"]
    assert report["tasks_compressed"] == 3
    assert report["errors"] == []


def test_curator_compresses_without_losing_a_single_task(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    for i in range(6):
        _task(archive, "CEO", f"t{i}")

    ArchiveCurator(archive, keep_tasks=2, keep_trade_days=3).run()

    history = archive.agent_tasks("CEO", limit=0)
    ids = [row["task_id"] for row in history]

    assert sorted(ids) == [f"t{i}" for i in range(6)]
    # the deliverables survive compression, so the agent can still learn from them
    assert all(row["result"]["deliverable"] for row in history)

    compressed = list((archive.agent_dir("CEO") / "tasks" / "compressed").glob("*.jsonl.gz"))
    assert len(compressed) == 1
    with gzip.open(compressed[0], "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    assert len(rows) == 4


def test_curator_is_idempotent(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    for i in range(6):
        _task(archive, "CEO", f"t{i}")

    curator = ArchiveCurator(archive, keep_tasks=2, keep_trade_days=3)
    first = curator.run()
    second = curator.run()

    assert first["tasks_compressed"] == 4
    assert second["tasks_compressed"] == 0
    assert len(archive.agent_tasks("CEO", limit=0)) == 6


def test_curator_never_deletes_a_task_before_the_gzip_is_written(tmp_path, monkeypatch):
    archive = Archive(str(tmp_path / "archiv"))
    for i in range(4):
        _task(archive, "CEO", f"t{i}")

    def broken_append(path, rows):
        raise OSError("disk full")

    monkeypatch.setattr(archive, "append_gzip_jsonl", broken_append)

    report = ArchiveCurator(archive, keep_tasks=1, keep_trade_days=3).run()

    assert report["errors"]
    # nothing was lost: all four originals are still on disk
    assert len(list((archive.agent_dir("CEO") / "tasks").glob("*.json"))) == 4


def test_curator_gzips_old_trade_days_and_keeps_them_readable(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    archive.ensure()

    old_day = date.today() - timedelta(days=10)
    old_name = old_day.strftime("%Y-%m-%d")
    old_dir = archive.trades_dir / old_name
    old_dir.mkdir(parents=True, exist_ok=True)

    fill = _trade_event(f"{old_name}T10:00:00+00:00", "FILLED", "d1")
    close = _trade_event(
        f"{old_name}T11:00:00+00:00", "CLOSED", "d1",
        price=80100.0, profit=1.5, profit_currency="CHF",
    )
    (old_dir / "100000_BTCUSD_SELL_FILLED_d1.json").write_text(json.dumps(fill), encoding="utf-8")
    (old_dir / "110000_BTCUSD_SELL_CLOSED_d1.json").write_text(json.dumps(close), encoding="utf-8")

    report = ArchiveCurator(archive, keep_tasks=10, keep_trade_days=3).run()

    assert report["trade_events_compressed"] == 2
    assert report["trade_days_removed"] == 1
    assert not old_dir.exists()

    compressed = archive.compressed_dir / f"{old_day.strftime('%Y-%m')}.jsonl.gz"
    assert compressed.exists()

    trades = archive.trades()
    assert len(trades) == 1
    assert trades[0]["status"] == "closed"
    assert trades[0]["profit"] == 1.5


def test_curator_keeps_recent_trade_days_as_plain_files(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    archive.ensure()

    today_dir = archive.trades_dir / date.today().strftime("%Y-%m-%d")
    today_dir.mkdir(parents=True, exist_ok=True)
    row = _trade_event(f"{date.today().strftime('%Y-%m-%d')}T10:00:00+00:00", "FILLED", "d1")
    (today_dir / "100000_BTCUSD_SELL_FILLED_d1.json").write_text(json.dumps(row), encoding="utf-8")

    report = ArchiveCurator(archive, keep_tasks=10, keep_trade_days=3).run()

    assert report["trade_events_compressed"] == 0
    assert report["trade_days_removed"] == 0
    assert today_dir.exists()


def test_curator_writes_the_index(tmp_path):
    archive = Archive(str(tmp_path / "archiv"))
    _task(archive, "RiskOfficer", "t1")

    ArchiveCurator(archive, keep_tasks=10, keep_trade_days=3).run()

    index = (archive.root / "INDEX.md").read_text(encoding="utf-8")
    assert "# Archive index" in index
    assert "RiskOfficer" in index
    assert "keep the latest 10 task files per agent" in index
