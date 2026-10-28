import pytest

from src.firm.agents.memory import LessonStore


@pytest.fixture
def store(tmp_path):
    return LessonStore(str(tmp_path / "lessons.db"))


def test_add_and_recent(store):
    created = store.add("CEO", "Always check the drawdown before proposing new work.", confidence=0.7)
    assert created is not None
    assert store.count("CEO") == 1

    lessons = store.recent("CEO")
    assert lessons[0]["lesson"].startswith("Always check")
    assert lessons[0]["confidence"] == pytest.approx(0.7)


def test_duplicate_raises_confidence_instead_of_duplicating(store):
    lesson = "Verify data freshness before drawing conclusions."
    assert store.add("QuantDeveloper", lesson) is not None
    assert store.add("QuantDeveloper", lesson) is None  # duplicate

    assert store.count("QuantDeveloper") == 1
    assert store.recent("QuantDeveloper")[0]["confidence"] > 0.6


def test_rejects_empty_short_and_none(store):
    assert store.add("CEO", "NONE") is None
    assert store.add("CEO", "too short") is None
    assert store.add("CEO", "") is None
    assert store.count("CEO") == 0


def test_lessons_are_isolated_per_agent(store):
    store.add("CEO", "Set one clear priority per cycle.")
    store.add("RiskOfficer", "Tighten limits before adding exposure.")
    assert store.count("CEO") == 1
    assert store.count("RiskOfficer") == 1
    assert store.recent("CEO")[0]["lesson"].startswith("Set one clear")


def test_store_is_bounded_per_agent(tmp_path):
    store = LessonStore(str(tmp_path / "bounded.db"), max_per_agent=2)
    store.add("CEO", "Lesson number one about priorities.")
    store.add("CEO", "Lesson number two about focus.")
    store.add("CEO", "Lesson number three about delegation.")
    assert store.count("CEO") == 2


def test_record_applied(store):
    created = store.add("CEO", "Check open tasks before delegating more.")
    store.record_applied([created["lesson_id"]])
    store.record_applied([created["lesson_id"]])
    assert store.recent("CEO")[0]["applied_count"] == 2


def test_expired_lessons_are_not_injected_and_can_be_purged(tmp_path):
    store = LessonStore(str(tmp_path / "ttl.db"), ttl_days=-1)
    assert store.add("CEO", "This lesson is already expired for testing.") is not None
    assert store.count("CEO") == 1
    assert store.recent("CEO") == []  # never injected into a prompt

    assert store.purge_expired() == 1
    assert store.count("CEO") == 0


def test_lesson_order_prefers_confidence(store):
    store.add("CEO", "Low confidence note about something.", confidence=0.3)
    store.add("CEO", "High confidence note about priorities.", confidence=0.9)
    assert store.recent("CEO")[0]["lesson"].startswith("High confidence")
