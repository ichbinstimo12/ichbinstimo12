"""Markt-Scanner (Money-Zone, deterministisch, sendet nie Orders).

Ziel: nicht nur die vier Whitelist-Maerkte, sondern die ganze Capital.com-Palette im Blick behalten.

  Universum   alle `universe_hours` die komplette Marktliste (GET /markets) laden, handelbare Maerkte nach
              Kosten (Spread in %) und Bewegung (Tagesveraenderung / Spread) sortieren. Whitelist zuerst, dann die
              besten Kandidaten bis zur Stream-Grenze (40 Epics laut Capital.com-Doku) in den Live-Kursstream.
  Sekundentakt  jede Sekunde: Stream-Kurse pruefen; ist eine Kerze geschlossen (z. B. alle 5 Minuten), werden die
              Kerzen aller Universums-Maerkte geholt (gedrosselt, `rps` Anfragen/s, Capital.com erlaubt 10/s) und
              alle Daytrading-Strategien mit Standardparametern geprueft, dazu die TJR-/SMC-Lage.
  Ergebnis    SCANNER_HIT (neues Signal auf einer frisch geschlossenen Kerze) und SMC_SETUP (TJR-Stufe wechselt:
              sweep -> mss -> fvg -> entry). Die TJR-Agenten werden davon geweckt.

Gehandelt wird weiterhin NUR, was in config/whitelist.toml steht UND die Forschung bestanden hat. Ein Treffer
ausserhalb der Whitelist ist eine Information (und ein Forschungskandidat), kein Auftrag.
Signale werden nur auf geschlossenen Kerzen bewertet (wie im Backtest); ein Sekundentakt erzeugt deshalb nicht mehr
gueltige Signale, verkuerzt aber die Zeit zwischen Kerzenschluss und Reaktion auf wenige Sekunden.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from src.firm.research import lab

logger = logging.getLogger("MarketScanner")

STREAM_LIMIT = 40
SETTLE_SECONDS = 2.0          # Capital.com braucht einen Moment, bis die geschlossene Kerze geliefert wird
SKIP_STRATEGIES = ("ml_logit",)   # lernt erst in der Forschung; mit Standardgewichten waere es Zufall


def _num(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def pick_universe(markets: List[Dict[str, Any]], exclude: List[str], limit: int,
                  max_spread_pct: float = 0.1) -> List[Dict[str, Any]]:
    """Handelbare Maerkte mit kleinem Spread, sortiert nach Bewegung je Kosteneinheit."""
    out = []
    for mk in markets:
        epic = mk.get("epic")
        bid, ask = _num(mk.get("bid")), _num(mk.get("offer"))
        if not epic or epic in exclude or bid is None or ask is None or bid <= 0 or ask < bid:
            continue
        if str(mk.get("marketStatus") or "TRADEABLE").upper() != "TRADEABLE":
            continue
        spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
        if spread_pct > max_spread_pct:
            continue
        change = abs(_num(mk.get("percentageChange")) or 0.0)
        out.append({"epic": epic, "spread_pct": round(spread_pct, 5), "change_pct": change,
                    "score": change / max(spread_pct, 1e-4), "type": mk.get("instrumentType")})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:max(0, limit)]


SHADOW_MAX_BARS = 48          # Schatten-Trade endet spaetestens nach so vielen Kerzen (Daytrading: ~4 h bei 5 min)
MAX_OPEN_SHADOWS = 500


def resolve_shadow(sh: Dict[str, Any], bars: List[Dict[str, Any]], bar_seconds: float) -> Optional[Dict[str, Any]]:
    """Was waere passiert? Kerzen NACH dem Signal durchgehen: Stop oder Ziel zuerst (beides in einer Kerze = Stop,
    konservativ), sonst nach max_bars zum Schlusskurs. R abzueglich Spread-Kosten. None = noch offen."""
    risk = abs(sh["entry"] - sh["stop"])
    if risk <= 0:
        return {"outcome": "invalid", "r": 0.0, "bars": 0}
    d = 1 if sh["side"] == "BUY" else -1
    after = [b for b in bars if b["ts"] > sh["bar_ts"]]
    for n, b in enumerate(after, 1):
        hit_stop = b["l"] <= sh["stop"] if d > 0 else b["h"] >= sh["stop"]
        hit_target = b["h"] >= sh["target"] if d > 0 else b["l"] <= sh["target"]
        if hit_stop:
            return {"outcome": "stop", "r": round(-1.0 - sh["cost_r"], 3), "bars": n}
        if hit_target:
            return {"outcome": "target", "r": round(abs(sh["target"] - sh["entry"]) / risk - sh["cost_r"], 3), "bars": n}
        if n >= sh["max_bars"]:
            return {"outcome": "time", "r": round(d * (b["c"] - sh["entry"]) / risk - sh["cost_r"], 3), "bars": n}
    return None


class MarketScanner:
    def __init__(self, broker: Any, stream: Any, event_store: Any, whitelist: List[str],
                 resolution: str = "MINUTE_5", universe_size: int = 36, universe_hours: float = 6.0,
                 rps: float = 2.0, max_spread_pct: float = 0.1, now_fn: Callable[[], float] = time.time,
                 session_fn: Optional[Callable[[str], dict]] = None):
        self.broker, self.stream, self.event_store = broker, stream, event_store
        self.whitelist = list(whitelist)
        self.resolution, self.bar = resolution, lab.RES_SECONDS[resolution]
        self.universe_size = min(universe_size, STREAM_LIMIT - len(self.whitelist))
        self.universe_hours, self.rps, self.max_spread_pct = universe_hours, max(rps, 0.1), max_spread_pct
        self.now = now_fn
        if session_fn is None:
            from src.firm.research.pipeline import load_session
            session_fn = load_session
        self.session_fn = session_fn
        self.universe: List[str] = list(self.whitelist)
        self.queue: List[str] = []
        self._budget = 0.0
        self._last_boundary = 0.0
        self._last_universe = 0.0
        self._seen_hits: set = set()
        self.shadows: List[Dict[str, Any]] = []      # Schattenbuch: Signale verfolgen, als waeren sie gehandelt
        self._smc_stage: Dict[str, Optional[str]] = {}
        self.stats = {"markets_total": None, "scans": 0, "hits": 0, "last_scan": None, "errors": 0}

    # ------------------------------------------------------------------ universe
    async def refresh_universe(self) -> None:
        self._last_universe = self.now()
        try:
            markets = await self.broker.get_market_list()
        except Exception as e:
            markets = []
            logger.warning(f"Market list unavailable: {e}")
        if not markets:
            # Keep the current universe and retry in 5 minutes instead of shrinking to the whitelist for hours.
            self._last_universe = self.now() - self.universe_hours * 3600 + 300
            self.stats["errors"] += 1
            self.event_store.append("SCANNER", "UNIVERSE_UPDATED", {
                "markets_total": 0, "whitelist": self.whitelist, "candidates": None, "resolution": self.resolution,
                "note": "Marktliste nicht verfuegbar - bisheriges Universum bleibt, neuer Versuch in 5 min"})
            return
        picked = pick_universe(markets, self.whitelist, self.universe_size, self.max_spread_pct)
        self.universe = self.whitelist + [p["epic"] for p in picked]
        if self.stream is not None:
            await self.stream.set_epics(self.universe)
        self.stats["markets_total"] = len(markets)
        self.event_store.append("SCANNER", "UNIVERSE_UPDATED", {
            "markets_total": len(markets), "whitelist": self.whitelist, "candidates": picked,
            "resolution": self.resolution,
            "note": "Marktliste leer - nur Whitelist" if not markets else "Kandidaten nach Spread und Bewegung"})
        logger.info(f"Scanner universe: {len(self.universe)} of {len(markets)} markets")

    # ------------------------------------------------------------------ 1-second tick
    async def tick(self) -> None:
        now = self.now()
        if now - self._last_universe >= self.universe_hours * 3600:
            await self.refresh_universe()
        boundary = (now // self.bar) * self.bar
        if boundary > self._last_boundary and now - boundary >= SETTLE_SECONDS:
            self._last_boundary = boundary
            self.queue = [e for e in self.universe if e not in self.queue] + self.queue
        self._budget = min(self._budget + self.rps, max(self.rps, 1.0))   # token bucket: rps < 1 works too
        while self.queue and self._budget >= 1.0:
            self._budget -= 1.0
            epic = self.queue.pop(0)
            try:
                await self.scan_epic(epic)
            except Exception as e:
                self.stats["errors"] += 1
                logger.warning(f"Scan {epic} failed: {type(e).__name__}: {e}")

    async def scan_epic(self, epic: str) -> List[Dict[str, Any]]:
        candles = await self.broker.get_candles(epic, resolution=self.resolution, limit=200)
        now = self.now()
        cs = [c for c in lab.normalize(candles) if c["ts"] + self.bar <= now + 1]   # nur geschlossene Kerzen
        if not cs:
            self.stats["errors"] += 1                        # broker returned nothing: not a healthy scan
            return []
        self.stats["scans"] += 1
        self.stats["last_scan"] = now
        if len(cs) < 40:
            return []
        if now - (cs[-1]["ts"] + self.bar) > 2 * self.bar:
            return []                                        # market closed / stale data: no "fresh" signals
        session = self.session_fn(epic)
        ind = lab.Ind(cs)
        i = ind.n - 1
        bar_ts = cs[i]["ts"]
        quote = self.stream.quote(epic, 30) if self.stream is not None else None
        spread_pct = None
        if quote and quote.get("bid") and quote.get("ask"):
            spread_pct = round((quote["ask"] - quote["bid"]) / ((quote["ask"] + quote["bid"]) / 2) * 100, 5)
        self._smc_update(epic, session, ind, bar_ts)
        self._resolve_shadows(epic, cs)
        hits = []
        for name in lab.candidates_for(self.resolution):
            if name in SKIP_STRATEGIES or (epic, name, bar_ts) in self._seen_hits:
                continue
            strat = lab.REGISTRY[name](**session)
            if not strat.can_enter_at(now):
                continue
            sig = strat.entry(ind, i)
            if not sig or strat.entry(ind, i - 1):
                continue
            self._seen_hits.add((epic, name, bar_ts))
            hit = {"epic": epic, "strategy": name, "side": sig[0], "bar_ts": bar_ts, "price": cs[i]["c"],
                   "spread_pct": spread_pct, "whitelisted": epic in self.whitelist, "resolution": self.resolution,
                   "note": "Standardparameter; gehandelt wird nur mit Forschungsfreigabe und Whitelist"}
            hits.append(hit)
            self.event_store.append("SCANNER", "SCANNER_HIT", hit)
            self._open_shadow(hit, sig, quote)
        self.stats["hits"] += len(hits)
        if len(self._seen_hits) > 5000:
            self._seen_hits = {h for h in self._seen_hits if h[2] >= bar_ts - 86400}
        return hits

    def _open_shadow(self, hit: Dict[str, Any], sig, quote) -> None:
        side, sd, tp = sig
        if sd <= 0 or len(self.shadows) >= MAX_OPEN_SHADOWS:
            return
        d = 1 if side == "BUY" else -1
        entry = hit["price"]
        spread = (quote["ask"] - quote["bid"]) if quote and quote.get("ask") and quote.get("bid") else 0.0
        self.shadows.append({"epic": hit["epic"], "strategy": hit["strategy"], "side": side, "entry": entry,
                             "stop": entry - d * sd, "target": entry + d * (tp if tp else 2 * sd),
                             "bar_ts": hit["bar_ts"], "cost_r": round(spread / sd, 3), "max_bars": SHADOW_MAX_BARS,
                             "whitelisted": hit["whitelisted"]})

    def _resolve_shadows(self, epic: str, cs: List[Dict[str, Any]]) -> None:
        still = []
        for sh in self.shadows:
            out = resolve_shadow(sh, cs, self.bar) if sh["epic"] == epic else None
            if out is None:
                still.append(sh)
                continue
            self.event_store.append("SCANNER", "SHADOW_RESULT", {
                **{k: sh[k] for k in ("epic", "strategy", "side", "entry", "stop", "target", "whitelisted")},
                **out, "resolution": self.resolution,
                "note": "Schatten-Trade (nicht gehandelt), Standardparameter, Spread abgezogen"})
        self.shadows = still

    def _smc_update(self, epic: str, session: dict, ind, bar_ts: float) -> None:
        ctx = lab.REGISTRY["smc_tjr"](**session).context(ind)
        stage = ctx.get("stage")
        if stage == self._smc_stage.get(epic):
            return
        had_setup = self._smc_stage.get(epic) is not None
        self._smc_stage[epic] = stage
        if stage is None and not had_setup:
            return
        self.event_store.append("SCANNER", "SMC_SETUP", {
            "epic": epic, "stage": stage, "side": ctx.get("side"), "bar_ts": bar_ts,
            "whitelisted": epic in self.whitelist, "levels": {k: ctx.get(k) for k in (
                "pre_session_high", "pre_session_low", "prev_day_high", "prev_day_low", "sweep_extreme",
                "structure_level", "fvg")}})

    def status(self) -> Dict[str, Any]:
        return {**self.stats, "universe": len(self.universe), "queue": len(self.queue), "resolution": self.resolution}

    async def run(self, interval: float = 1.0) -> None:
        logger.info(f"Market scanner started (every {interval:g}s, {self.resolution} bars).")
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats["errors"] += 1
                logger.error(f"Scanner tick error: {e}")
            await asyncio.sleep(interval)
