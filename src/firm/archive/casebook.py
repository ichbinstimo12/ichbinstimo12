"""Fallbuch des Archivars: jeder abgeschlossene Trade als Lernfall mit seinem Kontext.

Fall = TRADE_RESULT + die dazugehoerige Ausfuehrung (ORDER_FILLED), das Signal (Strategie, Grund, Stop, Ziel,
Zeitrahmen), die TJR/SMC-Stufe beim Einstieg, Schlagzeilen zum Markt in der Stunde davor und High-Impact-Termine
in der Naehe. Alles stammt aus dem Event-Log; fehlt etwas, steht dort None - es wird nichts geschaetzt.

Daraus entstehen Muster je Gruppe (Strategie, Markt, Sitzung, Nachrichtenlage, Spur, SMC-Stufe) mit Anzahl,
Trefferquote und durchschnittlichem R, jeweils mit den Trade-IDs als Beleg. Kleine Stichproben werden ausdruecklich
als solche markiert: Muster sind Lernhinweise fuer die Agenten, kein Beweis und keine Handelsregel. Aendern koennen die
Agenten damit nur in Richtung weniger Risiko (tighten_risk / block_entries).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Casebook")

NEWS_WINDOW_MIN = 60
CAL_WINDOW_MIN = 60
SESSIONS = (("Asia", 0, 7), ("London", 7, 12), ("New York", 12, 17), ("Spaet", 17, 24))


def _p(e: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(e.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}


def _ts(s: str) -> float:
    return datetime.fromisoformat(str(s).replace("Z", "")).replace(tzinfo=timezone.utc).timestamp()


def session_bucket(ts: float) -> str:
    """Asia (0-7 UTC), London (7-12), New York (12-17), Spaet (17-24)."""
    h = datetime.fromtimestamp(ts, timezone.utc).hour
    return next(name for name, a, b in SESSIONS if a <= h < b)


class Casebook:
    def __init__(self, event_store, archive_dir: str | Path = "archiv"):
        self.store = event_store
        self.path = Path(archive_dir) / "journal" / "cases.jsonl"

    def _journaled(self) -> set:
        return {_p(e).get("trade_id") for e in self.store.get_events(types=["JOURNAL_ENTRY"])}

    def _latest_before(self, types: List[str], epic: str, t: float, window_s: float, last: int = 500):
        for e in reversed(self.store.get_events(types=types, last=last)):
            te = _ts(e["ts"])
            if te <= t + 5 and t - te <= window_s and _p(e).get("epic") == epic:
                return e
        return None

    def build_case(self, result: Dict[str, Any]) -> Dict[str, Any]:
        p, t_res = _p(result), _ts(result["ts"])
        epic = p.get("epic")
        fill = self._latest_before(["ORDER_FILLED"], epic, t_res, 7 * 86400)
        t_fill = _ts(fill["ts"]) if fill else None
        sig = self._latest_before(["SIGNAL"], epic, t_fill, 3600) if t_fill else None
        smc = self._latest_before(["SMC_SETUP"], epic, t_fill, 4 * 3600) if t_fill else None
        sp, fp = _p(sig) if sig else {}, _p(fill) if fill else {}
        news, events = [], []
        if t_fill:
            since = datetime.fromtimestamp(t_fill - NEWS_WINDOW_MIN * 60, timezone.utc).replace(tzinfo=None).isoformat()
            for e in self.store.get_events(types=["NEWS_ALERT"], after_ts=since):
                if _ts(e["ts"]) <= t_fill + 5 and epic in (_p(e).get("epics") or []):
                    news.append({"title": _p(e).get("title"), "source": _p(e).get("source"), "link": _p(e).get("link")})
            cal = [e for e in self.store.get_events(types=["ECON_CALENDAR"], last=20) if _ts(e["ts"]) <= t_fill + 5]
            if cal:
                for ev in _p(cal[-1]).get("high_impact") or []:
                    if abs(float(ev.get("ts") or 0) - t_fill) <= CAL_WINDOW_MIN * 60:
                        events.append(f"{ev.get('country')} {ev.get('title')} {ev.get('time_utc')}")
        return {
            "trade_id": result["id"], "epic": epic, "side": p.get("side"), "strategy": p.get("strategy") or sp.get("strategy"),
            "lane": p.get("lane") or sp.get("lane"), "resolution": sp.get("resolution"),
            "opened": fill["ts"] if fill else None, "closed": result["ts"],
            "minutes_held": round((t_res - t_fill) / 60, 1) if t_fill else None,
            "entry": fp.get("price"), "stop": sp.get("stop_loss"), "target": sp.get("take_profit"),
            "reason": sp.get("reason"), "r": p.get("r"), "pnl": p.get("pnl"), "exact": p.get("exact"),
            "session": session_bucket(t_fill) if t_fill else None,
            "weekday": datetime.fromtimestamp(t_fill, timezone.utc).strftime("%a") if t_fill else None,
            # SMC stage only describes smc_tjr trades (an ORB trade near an old sweep is not an "SMC case")
            "smc_stage": (_p(smc).get("stage") if smc and (p.get("strategy") or sp.get("strategy")) == "smc_tjr"
                          else None),
            "news_before": news[-3:], "news_nearby": bool(news), "high_impact_nearby": events,
        }

    def sync(self) -> List[Dict[str, Any]]:
        """Neue Faelle fuer noch nicht erfasste TRADE_RESULTs anlegen (idempotent, inkrementell)."""
        if not hasattr(self, "_done"):
            self._done = self._journaled()
            self._cursor: Optional[str] = None
        new = []
        for res in self.store.get_events(types=["TRADE_RESULT"], after_ts=self._cursor):
            self._cursor = res["ts"]
            if res["id"] in self._done:
                continue
            self._done.add(res["id"])
            try:
                case = self.build_case(res)
            except Exception as e:                   # one odd result must not block all later cases
                logger.error(f"case for {res['id']} not built: {e}")
                continue
            self.store.append("ARCHIVIST", "JOURNAL_ENTRY", case, correlation_id=res["id"])
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(case, default=str) + "\n")
            except OSError as e:
                logger.warning(f"case file not written: {e}")
            new.append(case)
        return new

    def cases(self, last: int = 500) -> List[Dict[str, Any]]:
        return [_p(e) for e in self.store.get_events(types=["JOURNAL_ENTRY"], last=last)]


DIMENSIONS = ("strategy", "epic", "session", "news_nearby", "lane", "smc_stage")


def patterns(cases: List[Dict[str, Any]], min_n: int = 5) -> List[Dict[str, Any]]:
    """Gruppen mit mind. min_n Faellen (mit R): Anzahl, Trefferquote, Schnitt-R, Belege. Sortiert nach |Schnitt-R|."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for c in cases:
        # inexact R (another position was open, P&L from balance difference) is no evidence
        if not isinstance(c.get("r"), (int, float)) or c.get("exact") is False:
            continue
        for d in DIMENSIONS:
            if c.get(d) is not None:
                groups.setdefault(f"{d}={c[d]}", []).append(c)
    out = []
    for g, cs in groups.items():
        if len(cs) < min_n:
            continue
        rs = [c["r"] for c in cs]
        n = len(rs)
        out.append({"group": g, "n": n, "win_rate": round(sum(1 for r in rs if r > 0) / n, 2),
                    "avg_r": round(sum(rs) / n, 2), "total_r": round(sum(rs), 2),
                    "evidence": [c["trade_id"] for c in cs][-20:],
                    "note": "kleine Stichprobe - Hinweis, kein Beweis" if n < 30 else "Stichprobe >= 30"})
    out.sort(key=lambda p: abs(p["avg_r"]), reverse=True)
    return out


def journal_facts(event_store, last: int = 300) -> Optional[Dict[str, Any]]:
    """Kompakte Lernfakten fuer die Agenten (aus JOURNAL_ENTRY-Ereignissen)."""
    cases = [_p(e) for e in event_store.get_events(types=["JOURNAL_ENTRY"], last=last)]
    if not cases:
        return None
    return {
        "cases_total": len(cases),
        "patterns": patterns(cases)[:8],
        "last_cases": [{k: c.get(k) for k in ("trade_id", "epic", "strategy", "session", "r", "reason", "smc_stage",
                                              "news_nearby", "high_impact_nearby")} for c in cases[-4:]],
        "note": "Lernfaelle aus echten Trades; Muster unter 30 Faellen sind Hinweise, keine Regeln.",
    }
