"""Trade-Journal: echtes realisiertes P&L, Kennzahlen, Strategie-Gesundheit (Kill-Kriterien), Desk-Schutzregeln.

Warum: Im bisherigen Ledger steht `profit: null`. Ohne gemessenes P&L kann weder gelernt noch gestoppt werden.
Das P&L wird hier aus den echten Broker-Kontostaenden (ACCOUNT_SNAPSHOT) rekonstruiert.
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
from datetime import datetime, timezone


def _ts(s) -> float:
    return datetime.fromisoformat(str(s).replace("Z", "")).replace(tzinfo=timezone.utc).timestamp()


def load_events(db_path, types=None) -> list:
    c = sqlite3.connect(db_path); c.row_factory = sqlite3.Row
    q = "SELECT ts, agent_id, type, payload_json FROM events"
    a = []
    if types:
        q += " WHERE type IN (%s)" % ",".join("?" * len(types)); a = list(types)
    q += " ORDER BY ts"
    out = [{"ts": r["ts"], "agent": r["agent_id"], "type": r["type"], "p": json.loads(r["payload_json"])} for r in c.execute(q, a)]
    c.close()
    return out


def reconstruct_trades(events: list) -> list:
    """P&L je Trade = Broker-Kontostand (enthaelt offene Gewinne/Verluste) beim ersten Snapshot NACH der erkannten
    Schliessung minus Kontostand direkt VOR dem Einstieg. Exakt, solange sich Trades nicht ueberlappen (`exact`)."""
    snaps = [(_ts(e["ts"]), e["p"]) for e in events if e["type"] == "ACCOUNT_SNAPSHOT" and e["p"].get("balance") is not None]
    signals = [e for e in events if e["type"] == "SIGNAL"]
    fills = [e for e in events if e["type"] == "ORDER_FILLED"]
    closes = [e for e in events if e["type"] == "POSITION_CLOSED"]
    used, trades = set(), []
    for k, f in enumerate(fills):
        p, t0 = f["p"], _ts(f["ts"])
        sig = next((s for s in reversed(signals) if s["p"].get("epic") == p.get("epic") and _ts(s["ts"]) <= t0 + 5), None)
        cl = next((c for c in closes if id(c) not in used and _ts(c["ts"]) >= t0 and c["p"].get("epic") == p.get("epic")
                   and str(c["p"].get("direction", c["p"].get("side"))).upper() == str(p.get("side")).upper()), None)
        t = {"epic": p.get("epic"), "side": p.get("side"), "size": p.get("size"), "entry": p.get("price"), "opened": f["ts"],
             "reason_open": (sig or {}).get("p", {}).get("reason"), "stop_dist": None, "risk": None,
             "pnl": None, "r": None, "exact": None, "status": "OPEN", "minutes": None}
        if sig and sig["p"].get("price") and sig["p"].get("stop_loss"):
            t["stop_dist"] = abs(sig["p"]["price"] - sig["p"]["stop_loss"])
            t["risk"] = t["stop_dist"] * (p.get("size") or 0)
        if cl:
            used.add(id(cl))
            tc = _ts(cl["ts"])
            before = [b for (s, b) in snaps if s <= t0 + 1]
            after = next((b for (s, b) in snaps if s > tc), None)
            t["closed"], t["minutes"], t["status"] = cl["ts"], (tc - t0) / 60, "CLOSED"
            if before and after:
                t["pnl"] = after["balance"] - before[-1]["balance"]
                if t["risk"]:
                    t["r"] = t["pnl"] / t["risk"]
            nxt = fills[k + 1] if k + 1 < len(fills) else None
            t["exact"] = not (nxt and _ts(nxt["ts"]) < tc)
        trades.append(t)
    return trades


def stats(trades: list) -> dict:
    cl = [t for t in trades if t.get("pnl") is not None]
    if not cl:
        return {"closed": 0}
    pnl = [t["pnl"] for t in cl]
    rs = [t["r"] for t in cl if t.get("r") is not None]
    w, l = [x for x in pnl if x > 0], [x for x in pnl if x <= 0]
    out = {"closed": len(cl), "net_pnl": sum(pnl), "win_rate": len(w) / len(cl), "avg_win": (sum(w) / len(w)) if w else 0.0,
           "avg_loss": (sum(l) / len(l)) if l else 0.0, "profit_factor": (sum(w) / -sum(l)) if l and sum(l) < 0 else None,
           "expectancy": sum(pnl) / len(cl)}
    if rs:
        out["expectancy_r"] = sum(rs) / len(rs)
    mins = [t["minutes"] for t in cl if t.get("minutes") is not None]
    if mins:
        out["median_minutes"] = statistics.median(mins)
    # Wie sicher ist die Aussage? Mit n Trades ist die Streuung der Gewinnquote riesig.
    n, p = len(cl), len(w) / len(cl)
    se = math.sqrt(max(p * (1 - p), 1e-9) / n)
    out["win_rate_95ci"] = [max(0.0, p - 1.96 * se), min(1.0, p + 1.96 * se)]
    out["verdict_possible"] = n >= 100
    return out


def equity_summary(events: list) -> dict:
    s = [(_ts(e["ts"]), e["p"]["equity"]) for e in events if e["type"] == "ACCOUNT_SNAPSHOT" and e["p"].get("equity") is not None]
    if not s:
        return {}
    eq = [v for _, v in s]
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v); dd = max(dd, (peak - v) / peak)
    return {"start": eq[0], "end": eq[-1], "change": eq[-1] - eq[0], "change_pct": (eq[-1] / eq[0] - 1) * 100,
            "max_drawdown_pct": dd * 100, "hours": (s[-1][0] - s[0][0]) / 3600}


class StrategyHealth:
    """Automatische Kill-Kriterien. Vorab festgelegt, nicht verhandelbar. status: OK | WARN | STOP."""
    def __init__(self, expected_expectancy_r=0.0, min_trades=20, stop_expectancy_r=-0.30, max_consecutive_losses=6,
                 max_live_dd_r=10.0):
        self.exp, self.min_trades = expected_expectancy_r, min_trades
        self.stop_e, self.max_cl, self.max_dd = stop_expectancy_r, max_consecutive_losses, max_live_dd_r

    def check(self, trade_rs: list) -> dict:
        n = len(trade_rs)
        cl = 0
        for r in reversed(trade_rs):
            if r <= 0: cl += 1
            else: break
        eq = peak = dd = 0.0
        for r in trade_rs:
            eq += r; peak = max(peak, eq); dd = max(dd, peak - eq)
        reasons, status = [], "OK"
        if cl >= self.max_cl:
            status = "STOP"; reasons.append(f"{cl} Verluste in Folge (>= {self.max_cl})")
        if dd >= self.max_dd:
            status = "STOP"; reasons.append(f"Live-Drawdown {dd:.1f} R >= {self.max_dd} R")
        if n >= self.min_trades:
            e = sum(trade_rs) / n
            if e <= self.stop_e:
                status = "STOP"; reasons.append(f"Live-Erwartungswert {e:.2f} R <= {self.stop_e} R nach {n} Trades")
            elif e < self.exp - 0.25 and status != "STOP":
                status = "WARN"; reasons.append(f"Live-Erwartungswert {e:.2f} R deutlich unter Backtest ({self.exp:.2f} R)")
        return {"status": status, "reasons": reasons, "n": n, "consecutive_losses": cl, "dd_r": dd}


class DeskRules:
    """Schutzregeln gegen Uebertrading und Rache-Trades (zustandsbehaftet, in-memory; optional JSON-persistierbar)."""
    def __init__(self, max_trades_per_day=4, max_consecutive_losses=3, pause_hours_after_losses=24,
                 cooldown_bars_after_stop=6, bar_seconds=3600):
        self.max_day, self.max_cl, self.pause_h = max_trades_per_day, max_consecutive_losses, pause_hours_after_losses
        self.cd_bars, self.bar_s = cooldown_bars_after_stop, bar_seconds
        self.state = {"day": None, "n_today": 0, "consec_losses": 0, "paused_until": 0.0, "last_loss": {}}

    def dump(self): return json.dumps(self.state)
    def load(self, s): self.state.update(json.loads(s))

    def _roll(self, now):
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if self.state["day"] != day:
            self.state["day"], self.state["n_today"] = day, 0

    def can_enter(self, epic: str, side: str, now: float | None = None):
        now = now or time.time()
        self._roll(now)
        if now < self.state["paused_until"]:
            return False, "Pause nach Verlustserie bis " + datetime.fromtimestamp(self.state["paused_until"], timezone.utc).strftime("%d.%m. %H:%M UTC")
        if self.state["n_today"] >= self.max_day:
            return False, f"Tageslimit {self.max_day} Trades erreicht"
        last = self.state["last_loss"].get(f"{epic}:{side}")
        if last and now - last < self.cd_bars * self.bar_s:
            return False, f"Abkuehlung nach Stop in {epic} {side} ({self.cd_bars} Kerzen)"
        return True, "ok"

    def on_entry(self, now=None):
        self._roll(now or time.time())
        self.state["n_today"] += 1

    def on_close(self, epic, side, pnl, now=None):
        now = now or time.time()
        if pnl is not None and pnl <= 0:
            self.state["consec_losses"] += 1
            self.state["last_loss"][f"{epic}:{side}"] = now
            if self.state["consec_losses"] >= self.max_cl:
                self.state["paused_until"] = now + self.pause_h * 3600
                self.state["consec_losses"] = 0
        elif pnl is not None:
            self.state["consec_losses"] = 0
