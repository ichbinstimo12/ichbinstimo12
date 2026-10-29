import pytest

from src.firm.agents.delegation import DELEGATION_PLAN, build_delegation_tasks
from src.firm.agents.task_queue import DONE, FAILED, PENDING, RUNNING, TaskQueue
from src.firm.bus.event_store import EventStore


@pytest.fixture
def queue(tmp_path):
    store = EventStore(str(tmp_path / "tasks.db"))
    return TaskQueue(store.db_path, store)


def test_create_and_claim(queue):
    task = queue.create_task("CEO", "QuantDeveloper", "Backtest", "Do a backtest")
    assert task["status"] == PENDING
    assert queue.count_by_status()[PENDING] == 1

    claimed = queue.next_pending("QuantDeveloper")
    assert claimed["task_id"] == task["task_id"]
    assert claimed["status"] == RUNNING

    # A task can only be claimed once
    assert queue.next_pending("QuantDeveloper") is None


def test_complete_and_fail(queue):
    done_task = queue.create_task("CEO", "QuantDeveloper", "A", "A")
    queue.next_pending("QuantDeveloper")
    queue.complete(done_task["task_id"], {"status": "success"})
    assert queue.get_task(done_task["task_id"])["status"] == DONE

    failed_task = queue.create_task("CEO", "RiskOfficer", "B", "B")
    queue.next_pending("RiskOfficer")
    queue.fail(failed_task["task_id"], "boom")
    assert queue.get_task(failed_task["task_id"])["status"] == FAILED

    counts = queue.count_by_status()
    assert counts[DONE] == 1
    assert counts[FAILED] == 1


def test_task_transitions_are_audited(queue):
    task = queue.create_task("CEO", "QuantDeveloper", "Audit", "Audit me")
    queue.next_pending("QuantDeveloper")
    queue.complete(task["task_id"], {"status": "success"})

    types = [e["type"] for e in queue.event_store.get_events(correlation_id=task["task_id"])]
    assert "TASK_CREATED" in types
    assert "TASK_STARTED" in types
    assert "TASK_COMPLETED" in types


def test_requeue_stale_resets_running_tasks(queue):
    task = queue.create_task("CEO", "QuantDeveloper", "Crash", "Simulate a crash")
    queue.next_pending("QuantDeveloper")
    assert queue.count_by_status()[RUNNING] == 1

    assert queue.requeue_stale() == 1
    assert queue.count_by_status()[RUNNING] == 0
    assert queue.count_by_status()[PENDING] == 1

    # The task can be claimed again after recovery
    assert queue.next_pending("QuantDeveloper")["task_id"] == task["task_id"]


def test_delegation_plan_only_for_known_agents():
    tasks = build_delegation_tasks(["CEO", "QuantDeveloper", "NotARole"])
    assigned = [t["assigned_to"] for t in tasks]
    assert assigned == ["CEO", "QuantDeveloper"]
    for task in tasks:
        assert task["title"]
        assert task["description"]


def test_every_role_has_a_plan():
    import yaml
    from pathlib import Path
    roles = {yaml.safe_load(p.read_text(encoding="utf-8"))["name"] for p in Path("config/roles").glob("*.yaml")}
    assert {"CEO", "SMCAnalyst", "SMCTrader", "Archivist"} <= roles
    assert roles == set(DELEGATION_PLAN.keys())
