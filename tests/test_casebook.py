"""Fallbuch: jeder Trade mit Kontext als Lernmaterial, Muster mit Belegen, kein erfundener Wert."""
import json
from datetime import datetime, timezone

from src.firm.archive.casebook import Casebook, patterns, session_bucket
from src.firm.bus.event_store import EventStore


def _seed(store, epic="GOLD", r=-1.0, strategy="smc_tjr", news=True):
    store.append("NEWS", "NEWS_ALERT", {"title": f"Gold slides {r}", "source": "X", "epics": [epic], "link": "l"}) if news else None
    store.append("SCANNER", "SMC_SETUP", {"epic": epic, "stage": "entry", "side": "BUY"})
    store.append("TRADING_DESK", "SIGNAL", {"epic": epic, "side": "BUY", "price": 100.0, "stop_loss": 99.0,
                                            "take_profit": 102.0, "reason": "sweep+mss+fvg", "strategy": strategy,
                                            "lane": "EXPLORE", "resolution": "MINUTE_5"})
    store.append("EXECUTION_GATEWAY", "ORDER_FILLED", {"epic": epic, "side": "BUY", "size": 1, "price": 100.0})
    return store.append("TRADING_DESK", "TRADE_RESULT", {"epic": epic, "side": "BUY", "strategy": strategy,
                                                         "pnl": r * 10, "r": r, "lane": "EXPLORE", "exact": True})


def test_case_file_contains_the_context_of_the_trade(tmp_path):
    store = EventStore(str(tmp_path / "c.db"))
    tid = _seed(store)
    book = Casebook(store, archive_dir=tmp_path / "arch")
    new = book.sync()
    assert len(new) == 1
    c = new[0]
    assert c["trade_id"] == tid and c["strategy"] == "smc_tjr" and c["r"] == -1.0
    assert c["stop"] == 99.0 and c["reason"] == "sweep+mss+fvg" and c["smc_stage"] == "entry"
    assert c["news_before"] and c["news_nearby"] is True and c["session"] in session_bucket.__doc__
    assert book.sync() == []                                    # idempotent
    rows = (tmp_path / "arch" / "journal" / "cases.jsonl").read_text().splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["trade_id"] == tid
    assert len(store.get_events(types=["JOURNAL_ENTRY"])) == 1


def test_patterns_need_enough_trades_and_cite_evidence(tmp_path):
    store = EventStore(str(tmp_path / "c.db"))
    for r in (-1.0, -1.0, -0.8, -1.0, 0.5):
        _seed(store, r=r)
    book = Casebook(store, archive_dir=tmp_path / "arch")
    cases = book.sync()
    pats = patterns(cases, min_n=5)
    strat = next(p for p in pats if p["group"] == "strategy=smc_tjr")
    assert strat["n"] == 5 and strat["avg_r"] == -0.66 and len(strat["evidence"]) == 5
    assert "kein Beweis" in strat["note"]                       # small samples are labelled as such
    assert patterns(cases[:3], min_n=5) == []


def test_session_buckets():
    ts = lambda h: datetime(2026, 10, 29, h, 0, tzinfo=timezone.utc).timestamp()
    assert session_bucket(ts(3)) == "Asia" and session_bucket(ts(8)) == "London"
    assert session_bucket(ts(14)) == "New York" and session_bucket(ts(21)) == "Spaet"
