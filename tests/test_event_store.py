import pytest
import sqlite3
import os
from src.firm.bus.event_store import EventStore

@pytest.fixture
def event_store(tmp_path):
    db_file = tmp_path / "test_events.db"
    return EventStore(str(db_file))

def test_append_and_retrieve(event_store):
    payload = {"msg": "hello"}
    event_id = event_store.append("agent-1", "TEST_EVENT", payload)

    event = event_store.get_event(event_id)
    assert event is not None
    assert event["agent_id"] == "agent-1"
    assert event["type"] == "TEST_EVENT"

def test_correlation_id(event_store):
    corr_id = "corr-123"
    event_store.append("agent-1", "START", {"step": 1}, correlation_id=corr_id)
    event_store.append("agent-2", "END", {"step": 2}, correlation_id=corr_id)

    events = event_store.get_events(correlation_id=corr_id)
    assert len(events) == 2
    assert events[0]["type"] == "START"
    assert events[1]["type"] == "END"

def test_immutability_trigger(event_store):
    event_id = event_store.append("agent-1", "IMMUTABLE", {"val": 1})

    # Try to update the event via raw SQL
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        with sqlite3.connect(event_store.db_path) as conn:
            conn.execute("UPDATE events SET type = 'HACKED' WHERE id = ?", (event_id,))
            conn.commit()
    assert "Events are immutable and cannot be updated" in str(excinfo.value)

def test_deletion_trigger(event_store):
    event_id = event_store.append("agent-1", "IMMUTABLE", {"val": 1})

    # Try to delete the event via raw SQL
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        with sqlite3.connect(event_store.db_path) as conn:
            conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            conn.commit()
    assert "Events are immutable and cannot be deleted" in str(excinfo.value)
