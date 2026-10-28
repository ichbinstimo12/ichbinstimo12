"""Watch the learning loop: waits until agents have learned and applied lessons."""
import json
import sqlite3
import sys
import time

DB = "firm_events.db"
DEADLINE = time.time() + 480


def snapshot():
    conn = sqlite3.connect(DB, timeout=10)
    try:
        lessons = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        thoughts = conn.execute(
            "SELECT agent_id, payload_json FROM events WHERE type = 'THOUGHT'"
        ).fetchall()
        applied = 0
        for _, payload in thoughts:
            try:
                if int(json.loads(payload).get("lessons_applied") or 0) > 0:
                    applied += 1
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        done = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'TASK_COMPLETED'").fetchone()[0]
        learned_events = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'LEARNED'").fetchone()[0]
        return lessons, applied, done, learned_events
    finally:
        conn.close()


last = None
while time.time() < DEADLINE:
    try:
        lessons, applied, done, learned_events = snapshot()
    except Exception as exc:
        print(f"waiting ({exc})", flush=True)
        time.sleep(10)
        continue

    current = (lessons, applied, done, learned_events)
    if current != last:
        print(
            f"lessons={lessons} learned_events={learned_events} "
            f"thoughts_with_lessons={applied} tasks_done={done}",
            flush=True,
        )
        last = current

    if lessons > 0 and applied > 0:
        print("READY: agents learned and applied lessons", flush=True)
        sys.exit(0)

    time.sleep(8)

print("TIMEOUT: condition not reached", flush=True)
sys.exit(1)
