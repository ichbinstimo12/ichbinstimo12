"""Coach: aendert nur Signal-Parameter innerhalb der Grenzen, nur mit Backtest-Beleg, versioniert; Risiko nie."""
import json
import random

from src.firm.bus.event_store import EventStore
from src.firm.memedesk import coach, logic

CFG = logic.load_config()


def trade(narr, chase, pnl, structure=True):
    return {"type": "CLOSE", "pnl_pct": pnl, "signals": {"distribution": True, "structure": structure},
            "metrics": {"narrative": narr, "volume_growth": 2.0, "buy_ratio": 1.5, "holders_growth": 0.1,
                        "smart_buyers": 0, "m5_change": chase}}


def journal(tmp_path, rows):
    p = tmp_path / "journal.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p


def test_too_little_data_changes_nothing(tmp_path):
    res = coach.run_coach(CFG, journal(tmp_path, [trade(0.6, 5, 10)] * 5), tmp_path / "d")
    assert res["change"] is False and "zu wenig Daten" in res["reason"]


def test_learns_a_stricter_narrative_threshold_within_bounds_and_versions_it(tmp_path):
    random.seed(1)
    rows = [trade(0.65, 5, -25 + random.uniform(-3, 3), structure=False) for _ in range(20)] + \
           [trade(0.8, 5, 40 + random.uniform(-3, 3)) for _ in range(25)]
    store = EventStore(str(tmp_path / "c.db"))
    res = coach.run_coach(CFG, journal(tmp_path, rows), tmp_path / "d", store)
    assert res["change"] and res["version"] == 1
    lo, hi = CFG["bounds"]["narrative_min"]
    assert 0.65 < res["thresholds"]["narrative_min"] <= hi and res["backtest"]["new_avg_pct"] > 30
    saved = json.loads((tmp_path / "d" / "params_current.json").read_text())
    assert saved["version"] == 1 and "narrative_min" in saved["diff"]
    assert not any(k in json.dumps(saved) for k in ("risk_per_trade", "daily_loss", "drawdown", "hard_stop"))
    assert store.get_events(types=["MEME_COACH_CHANGE"])
    cfg2 = logic.load_config(override=tmp_path / "d" / "params_current.json")
    assert cfg2["thresholds"]["narrative_min"] == res["thresholds"]["narrative_min"]
