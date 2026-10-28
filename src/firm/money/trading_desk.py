"""
Deterministic trading desk.

This is the only component that turns market data into orders. It is
deliberately *not* an LLM: the Brain-Zone agents analyse, critique and learn,
but they can never size or place a trade. Everything here is arithmetic on real
broker data plus the hard limits in ``config/limits.toml``:

    candles -> signal -> position size -> RiskGate -> ExecutionGateway -> broker

Every decision is written to the immutable event store, so the dashboard and the
agents can see exactly why a trade happened (or why it did not).
"""

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.firm.bus.event_store import EventStore
from src.firm.money.execution_gateway import ExecutionGateway, TradeProposal
from src.firm.money.risk_gate import RiskGate
from src.firm.money.signal_v2 import SignalEngineV2
from src.firm.money.sizing import size_position_v2
from src.firm.money.strategy_gate import StrategyGate
from src.firm.money.journal import DeskRules, StrategyHealth
from src.firm.research.lab import RES_SECONDS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TradingDesk")

DEFAULT_RESOLUTION = "HOUR"  # v2: 1-Stunden-Kerzen (15-Min war Rauschen + Kostenfalle)
DEFAULT_LOOKBACK = 400  # genug Vorlauf fuer EMA200/Donchian


# --------------------------------------------------------------------- maths
def ema(values: List[float], period: int) -> float:
    """Exponential moving average of the last value in the series."""
    if not values or period <= 0:
        raise ValueError("ema needs values and a positive period")
    k = 2.0 / (period + 1.0)
    value = float(values[0])
    for price in values[1:]:
        value = float(price) * k + value * (1.0 - k)
    return value


def atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    """Average true range over the last `period` candles."""
    if len(candles) < 2:
        return 0.0
    true_ranges: List[float] = []
    for prev, cur in zip(candles[:-1], candles[1:]):
        high, low = float(cur["high"]), float(cur["low"])
        prev_close = float(prev["close"])
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    window = true_ranges[-period:]
    return sum(window) / len(window) if window else 0.0


def floor_to_step(value: float, step: float) -> float:
    """Round *down* to a broker size increment (never round a size up)."""
    if step <= 0:
        return value
    steps = math.floor((value + 1e-12) / step)
    return round(steps * step, 10)


# ------------------------------------------------------------------- signals
@dataclass
class Signal:
    epic: str
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    atr: float
    fast: float
    slow: float
    strength: float
    reason: str


class SignalEngine:
    """
    Trend-following signal on EMA fast/slow with an ATR stop.

    A signal only fires when the EMA gap is a meaningful fraction of volatility,
    so the desk stays flat in chop instead of overtrading. Stops are 1.5 ATR,
    targets 3 ATR (2:1 reward/risk).
    """

    def __init__(
        self,
        fast_period: int = 12,
        slow_period: int = 48,
        atr_period: int = 14,
        min_strength: float = 0.25,
        stop_atr: float = 1.5,
        target_atr: float = 3.0,
    ):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_period = atr_period
        self.min_strength = min_strength
        self.stop_atr = stop_atr
        self.target_atr = target_atr

    @property
    def warmup(self) -> int:
        return self.slow_period + self.atr_period

    def evaluate(
        self,
        epic: str,
        candles: List[Dict[str, Any]],
        bid: Optional[float] = None,
        ask: Optional[float] = None,
    ) -> Optional[Signal]:
        if len(candles) < self.warmup:
            return None

        closes = [float(c["close"]) for c in candles]
        fast = ema(closes, self.fast_period)
        slow = ema(closes, self.slow_period)
        volatility = atr(candles, self.atr_period)
        if volatility <= 0:
            return None

        last = closes[-1]
        gap = fast - slow
        if abs(gap) < self.min_strength * volatility:
            return None

        if gap > 0 and last > slow:
            side = "BUY"
            entry = float(ask) if ask else last
            stop = entry - self.stop_atr * volatility
            target = entry + self.target_atr * volatility
        elif gap < 0 and last < slow:
            side = "SELL"
            entry = float(bid) if bid else last
            stop = entry + self.stop_atr * volatility
            target = entry - self.target_atr * volatility
        else:
            return None

        if stop <= 0 or target <= 0:
            return None

        return Signal(
            epic=epic,
            side=side,
            entry=entry,
            stop_loss=stop,
            take_profit=target,
            atr=volatility,
            fast=fast,
            slow=slow,
            strength=abs(gap) / volatility,
            reason=(
                f"EMA{self.fast_period} {fast:.5f} vs EMA{self.slow_period} {slow:.5f} "
                f"({'above' if gap > 0 else 'below'}), ATR {volatility:.5f}, "
                f"strength {abs(gap) / volatility:.2f} ATR"
            ),
        )


# ------------------------------------------------------------------- sizing
def size_position(
    signal: Signal,
    balance: float,
    limits: Dict[str, Any],
    market: Dict[str, Any],
) -> Tuple[float, str]:
    """
    Risk-based sizing. Returns (size, reason); size == 0 means "do not trade".

    risk_budget = balance * max_risk_per_trade  (0.5% by default)
    size        = risk_budget / stop_distance
    then clamped to the broker's real min/max/step and to max_leverage.
    """
    if balance <= 0:
        return 0.0, "account balance unavailable"

    stop_distance = abs(signal.entry - signal.stop_loss)
    if stop_distance <= 0:
        return 0.0, "invalid stop distance"

    risk_budget = balance * float(limits["trade"]["max_risk_per_trade"])
    step = float(market.get("size_step") or 0.01)
    min_size = float(market.get("min_size") or step)
    max_size = float(market.get("max_size") or 0.0)

    raw = risk_budget / stop_distance
    if max_size > 0:
        raw = min(raw, max_size)
    size = floor_to_step(raw, step)

    # Never exceed the account leverage limit with one position.
    max_notional = balance * float(limits["account"]["max_leverage"])
    if signal.entry > 0:
        leverage_cap = floor_to_step(max_notional / signal.entry, step)
        size = min(size, leverage_cap)

    if size < min_size:
        return 0.0, (
            f"risk budget {risk_budget:.2f} is below broker minimum "
            f"{min_size} (stop distance {stop_distance:.6f})"
        )

    risk = stop_distance * size
    return size, (
        f"risk {risk:.2f} of budget {risk_budget:.2f} "
        f"(stop {stop_distance:.6f} x size {size})"
    )


# -------------------------------------------------------------- small state
class DeskState:
    """
    Tiny durable state: day start balance, peak equity, per-epic cooldowns.

    Used for the daily-loss and drawdown guards. It stores only numbers read
    from the broker, never estimates.
    """

    def __init__(self, path: str = "firm_state.json"):
        self.path = Path(path)
        self.data: Dict[str, Any] = {}
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(self.path)  # atomic: a crash never leaves a half-written state file
        except Exception as e:
            logger.warning(f"Could not persist desk state: {e}")

    def day_start_balance(self, today: str, balance: float) -> float:
        if self.data.get("day") != today or not self.data.get("day_start_balance"):
            self.data["day"] = today
            self.data["day_start_balance"] = balance
            self._save()
        return float(self.data["day_start_balance"])

    def peak_balance(self, equity: float) -> float:
        peak = float(self.data.get("peak_balance") or 0.0)
        if equity > peak:
            peak = equity
            self.data["peak_balance"] = peak
            self._save()
        return peak

    def cooldown_active(self, epic: str, now: float, seconds: float) -> bool:
        last = float((self.data.get("last_trade") or {}).get(epic) or 0.0)
        return seconds > 0 and (now - last) < seconds

    def mark_trade(self, epic: str, now: float) -> None:
        self.data.setdefault("last_trade", {})[epic] = now
        self._save()


# --------------------------------------------------------------------- desk
class TradingDesk:
    """Scans whitelisted markets and executes risk-approved trades."""

    def __init__(
        self,
        broker: Any,
        gateway: ExecutionGateway,
        risk_gate: RiskGate,
        event_store: EventStore,
        mode: str = "PAPER",
        whitelist_path: str = "config/whitelist.toml",
        limits_path: str = "config/limits.toml",
        resolution: str = DEFAULT_RESOLUTION,
        lookback: int = DEFAULT_LOOKBACK,
        cooldown_seconds: float = 0.0,
        state_path: str = "firm_state.json",
        engine: Optional[SignalEngine] = None,
        archive: Any = None,
        gate: Optional[StrategyGate] = None,
        rules: Optional[DeskRules] = None,
        overlay: Any = None,
        health: Optional[StrategyHealth] = None,
        halt_file: Optional[str] = None,
        stream: Any = None,
        quote_max_age: float = 15.0,
        max_api_errors: int = 10,
        api_error_window_s: float = 1800.0,
    ):
        self.broker = broker
        self.gateway = gateway
        self.risk_gate = risk_gate
        self.event_store = event_store
        self.mode = mode
        self.resolution = resolution
        self.lookback = lookback
        self.cooldown_seconds = cooldown_seconds
        self.engine = engine  # v2: pro Markt aus dem Forschungsurteil gewaehlt (siehe _engine_for)
        self.state = DeskState(state_path)
        self.gate = gate or StrategyGate()
        self.rules = rules or DeskRules(bar_seconds=RES_SECONDS.get(resolution, 3600))
        if self.state.data.get("rules"):
            try:
                self.rules.load(self.state.data["rules"])
            except Exception:
                pass
        self._engines: Dict[str, SignalEngineV2] = {}
        self._fx_cache: Dict[str, Tuple[float, float]] = {}
        self.archive = archive
        self.overlay = overlay
        self.health = health or StrategyHealth()
        self.last_summary: Dict[str, Any] = {}
        self._last_status_key: Optional[str] = None
        self.halt_file = Path(halt_file) if halt_file else None
        self.stream, self.quote_max_age = stream, quote_max_age
        self.max_api_errors, self.api_error_window_s = max_api_errors, api_error_window_s
        self._api_errors: List[float] = []
        self._api_halt_reported = False

        import tomllib
        with open(whitelist_path, "rb") as f:
            self.epics: List[str] = list(tomllib.load(f).get("allowed_epics") or [])
        with open(limits_path, "rb") as f:
            self.limits: Dict[str, Any] = tomllib.load(f)

        self._closed_market_logged: set = set()

    # ------------------------------------------------------------- v2 helpers
    def _engine_for(self, strategy: str, params: Optional[dict], resolution: Optional[str] = None) -> SignalEngineV2:
        res = resolution or self.resolution
        key = f"{strategy}:{res}:{json.dumps(params or {}, sort_keys=True)}"
        if key not in self._engines:
            self._engines[key] = SignalEngineV2(strategy, params or {}, resolution=res)
        return self._engines[key]

    def _save_rules(self) -> None:
        self.state.data["rules"] = self.rules.dump()
        self.state._save()

    async def _fx(self, quote: Optional[str], account_ccy: Optional[str]) -> Optional[float]:
        """Kurs Quote-Waehrung -> Kontowaehrung (None = unbekannt -> nicht handeln)."""
        if not quote or not account_ccy:
            return None
        if quote.upper() == account_ccy.upper():
            return 1.0
        key = f"{quote}{account_ccy}"
        now = datetime.utcnow().timestamp()
        if key in self._fx_cache and now - self._fx_cache[key][1] < 60:
            return self._fx_cache[key][0]
        for epic, inv in ((f"{quote}{account_ccy}", False), (f"{account_ccy}{quote}", True)):
            try:
                m = await self.broker.get_market_details(epic)
                bid, ask = m.get("bid"), m.get("ask")
                if bid and ask:
                    mid = (float(bid) + float(ask)) / 2.0
                    rate = 1.0 / mid if inv else mid
                    self._fx_cache[key] = (rate, now)
                    return rate
            except Exception:
                continue
        return None

    def _detect_closed(self, positions: List[Dict[str, Any]], balance: float) -> None:
        """Positionen, die der Broker per Stop/Ziel geschlossen hat -> Verlustserien-Regeln fuettern."""
        open_epics = {str(p.get("epic")) for p in positions}
        meta = self.state.data.setdefault("pos_meta", {})
        for epic in list(meta):
            if epic not in open_epics:
                m = meta.pop(epic)
                pnl = balance - float(m.get("balance_at_entry") or balance)
                risk = float(m.get("risk") or 0.0)
                r_mult = pnl / risk if risk > 0 else None
                self.rules.on_close(epic, m.get("side", ""), pnl)
                self.event_store.append("TRADING_DESK", "TRADE_RESULT", {
                    "epic": epic, "side": m.get("side"), "strategy": m.get("strategy"), "pnl": pnl, "r": r_mult,
                    "risk": risk or None, "lane": m.get("lane"), "balance_after": balance,
                    "exact": len(meta) == 0,
                    "note": "P&L = Kontostand jetzt - Kontostand bei Einstieg (exakt, wenn keine andere Position offen war)"})
                self._record_result(epic, m.get("strategy"), r_mult)
        self._save_rules()
        self.state._save()

    def _record_result(self, epic: str, strategy: Optional[str], r_mult: Optional[float]) -> None:
        """Kill-Kriterien je Markt+Strategie auf echten Live-R-Werten. STOP sperrt den Markt bis zum naechsten Urteil."""
        if r_mult is None or not strategy:
            return
        key = f"{epic}:{strategy}"
        hist = self.state.data.setdefault("results_r", {}).setdefault(key, [])
        hist.append(round(r_mult, 4))
        check = self.health.check(hist)
        if check["status"] == "STOP":
            self.state.data.setdefault("stopped", {})[key] = {
                "at": datetime.utcnow().timestamp(), "verdict_created": self.gate.created(), "reasons": check["reasons"]}
            self.event_store.append("TRADING_DESK", "STRATEGY_STOPPED", {"epic": epic, "strategy": strategy, **check})
            logger.critical(f"Kill-Kriterium: {key} gestoppt ({'; '.join(check['reasons'])})")
        elif check["status"] == "WARN":
            self.event_store.append("TRADING_DESK", "STRATEGY_WARNING", {"epic": epic, "strategy": strategy, **check})

    def _stopped(self, epic: str, strategy: str) -> Optional[str]:
        """Gestoppte Strategie bleibt gesperrt, bis ein NEUERES Forschungsurteil vorliegt (nur sperren, nie freischalten)."""
        s = (self.state.data.get("stopped") or {}).get(f"{epic}:{strategy}")
        if not s:
            return None
        if self.gate.created() > float(s.get("verdict_created") or 0):
            self.state.data["stopped"].pop(f"{epic}:{strategy}", None)
            self.state.data.get("results_r", {}).pop(f"{epic}:{strategy}", None)
            self.state._save()
            return None
        return "Kill-Kriterium aktiv: " + "; ".join(s.get("reasons") or [])

    def _stale(self, candles: List[Dict[str, Any]], resolution: Optional[str] = None) -> Optional[str]:
        """Stale-data check: the newest candle must be at most 3 bars old (prices frozen -> do not trade)."""
        if not candles:
            return "keine Kursdaten"
        from src.firm.research.lab import parse_ts
        last = parse_ts(candles[-1].get("t") or candles[-1].get("ts"))
        if last is None:
            return None  # format unknown (e.g. test doubles) -> the signal engine decides
        age = time.time() - last
        bar = RES_SECONDS.get(resolution or self.resolution, 3600)
        if age > 3 * bar + bar:
            return f"Kursdaten veraltet (letzte Kerze vor {age / 60:.0f} min)"
        return None

    def _api_error(self) -> None:
        self._api_errors.append(datetime.utcnow().timestamp())

    def _api_error_block(self) -> Optional[str]:
        """N API errors within M minutes -> no new orders until the window is clean again (spec: API error limit)."""
        now = datetime.utcnow().timestamp()
        self._api_errors = [t for t in self._api_errors if now - t < self.api_error_window_s]
        if len(self._api_errors) >= self.max_api_errors:
            reason = (f"API-Fehlerlimit: {len(self._api_errors)} Fehler in "
                      f"{self.api_error_window_s / 60:.0f} min - keine neuen Orders")
            if not self._api_halt_reported:
                self._api_halt_reported = True
                self.event_store.append("TRADING_DESK", "TRADING_HALTED", {"reason": reason, "action": "entries paused"})
            return reason
        self._api_halt_reported = False
        return None

    def _open_risk(self) -> float:
        """Sum of the money at risk (stop distance x size, account currency) of all positions the desk opened."""
        return sum(float(m.get("risk") or 0.0) for m in (self.state.data.get("pos_meta") or {}).values())

    async def _recover_fill(self, epic: str, side: str, size: float, known_deals: set,
                            proposal_reason: str) -> Optional[Dict[str, Any]]:
        """
        The order request failed (timeout, confirm error) - but did it land? Ask the broker. If a new position
        exists, book it like a fill; otherwise the reconciler would see 'unauthorised exposure' and kill the firm.
        """
        try:
            positions = await self.broker.get_positions()
        except Exception:
            return None
        for p in positions:
            if (str(p.get("epic") or "").upper() == epic.upper() and str(p.get("direction") or "").upper() == side
                    and str(p.get("dealId") or "") not in known_deals):
                filled = {"status": "FILLED", "order_id": p.get("dealId"), "filled_price": p.get("level"),
                          "filled_size": p.get("volume") or size}
                self.event_store.append("EXECUTION_GATEWAY", "ORDER_FILLED", {
                    "order_id": p.get("dealId"), "epic": epic, "side": side, "size": filled["filled_size"],
                    "price": p.get("level"), "recovered": True, "reason": proposal_reason})
                logger.warning(f"Order for {epic} errored but the position exists at the broker - booked as fill.")
                return filled
        return None

    def _publish_status(self, summary: Dict[str, Any]) -> None:
        """DESK_STATUS nur bei Aenderung der Gruende: Agenten und Dashboard sehen, WARUM nicht gehandelt wird."""
        reasons = {s.get("epic"): s.get("reason") for s in summary.get("skipped", [])}
        key = json.dumps([summary.get("error"), reasons], sort_keys=True, default=str)
        self.last_summary = {"ts": datetime.utcnow().isoformat(), "error": summary.get("error"), "reasons": reasons,
                             "trades": len(summary.get("trades", [])), "closed": len(summary.get("closed", []))}
        if key != self._last_status_key:
            self._last_status_key = key
            self.event_store.append("TRADING_DESK", "DESK_STATUS", self.last_summary)

    def _archive(self, kind: str, payload: Dict[str, Any]) -> None:
        """File the trade event. Archiving must never break execution."""
        if self.archive is None:
            return
        try:
            self.archive.record_trade(kind, payload)
        except Exception as e:
            logger.warning(f"Could not archive {kind}: {e}")

    # ------------------------------------------------------------- guards
    def trading_blocked(self) -> Optional[str]:
        """Return a reason string when the desk must not trade at all."""
        if self.mode.upper().startswith("LIVE") and not self.gateway._live_approved():
            return "live approval token missing or expired"
        if self.mode.upper() == "BACKTEST":
            return "backtest mode does not place orders"
        # A paper-mode desk must never send orders to the live environment.
        if not self.mode.upper().startswith("LIVE") and hasattr(self.broker, "is_demo"):
            if not self.broker.is_demo:
                return "paper mode with a LIVE broker connection - refusing to trade"
        if not getattr(self.broker, "place_order", None):
            return "broker does not support order placement (mock/offline)"
        if not self.epics:
            return "whitelist is empty"
        return None

    # -------------------------------------------------------------- cycle
    async def run_cycle(self) -> Dict[str, Any]:
        """One full scan: manage exits, then look for new entries."""
        summary = await self._run_cycle()
        try:
            self._publish_status(summary)
        except Exception as e:
            logger.warning(f"Desk status not published: {e}")
        return summary

    async def _run_cycle(self) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"trades": [], "skipped": [], "closed": [], "error": None}

        blocked = self.trading_blocked()
        if blocked:
            summary["error"] = blocked
            logger.warning(f"Trading desk idle: {blocked}")
            return summary

        api_block = self._api_error_block()
        if api_block:
            summary["error"] = api_block
            return summary

        try:
            account = await self.broker.get_account_details()
            balance = float(account.get("balance") or 0.0)
            equity = float(account.get("equity") or balance)
        except Exception as e:
            self._api_error()
            summary["error"] = f"account unavailable: {e}"
            logger.error(summary["error"])
            return summary

        # Limits are measured on EQUITY (includes open P&L) and the UTC trading day.
        today = datetime.now(timezone.utc).date().isoformat()
        day_start = self.state.day_start_balance(today, equity)
        peak = self.state.peak_balance(equity)
        daily_pnl = equity - day_start

        healthy, health_reason = self.risk_gate.check_account_health(equity, peak, daily_pnl, day_start)
        if not healthy:
            self.event_store.append(
                "TRADING_DESK", "TRADING_HALTED",
                {"reason": health_reason, "balance": balance, "equity": equity, "peak": peak,
                 "day_start": day_start, "daily_pnl": daily_pnl, "action": "flatten + halt (restart only by operator)"},
            )
            # Spec: breach -> flatten everything and stop; only the operator restarts (`firm resume`).
            if self.halt_file is not None:
                try:
                    self.halt_file.parent.mkdir(parents=True, exist_ok=True)
                    self.halt_file.write_text(f"{datetime.utcnow().isoformat()} {health_reason}", encoding="utf-8")
                except Exception as e:
                    logger.critical(f"Could not write halt flag: {e}")
            summary["error"] = health_reason
            logger.critical(f"Trading halted by account health: {health_reason}")
            return summary

        try:
            positions = await self.broker.get_positions()
        except Exception as e:
            self._api_error()
            summary["error"] = f"positions unavailable: {e}"
            logger.error(summary["error"])
            return summary

        self._detect_closed(positions, balance)
        await self._manage_exits(positions, summary)

        # Re-read after exits so the position count and open epics are current.
        try:
            positions = await self.broker.get_positions()
        except Exception:
            pass

        open_epics = {str(p.get("epic")) for p in positions}
        known_deals = {str(p.get("dealId")) for p in positions if p.get("dealId")}
        max_positions = int(self.limits["account"]["max_positions"])

        for epic in self.epics:
            if len(positions) >= max_positions:
                summary["skipped"].append({"epic": epic, "reason": "max positions reached"})
                continue
            if epic in open_epics:
                summary["skipped"].append({"epic": epic, "reason": "position already open"})
                continue
            if self.state.cooldown_active(epic, datetime.utcnow().timestamp(), self.cooldown_seconds):
                summary["skipped"].append({"epic": epic, "reason": "cooldown after last trade"})
                continue

            try:
                market = await self.broker.get_market_details(epic)
            except Exception as e:
                self._api_error()
                summary["skipped"].append({"epic": epic, "reason": f"market details unavailable: {e}"})
                continue

            if str(market.get("status") or "").upper() != "TRADEABLE":
                summary["skipped"].append({"epic": epic, "reason": f"market {market.get('status') or 'unknown'}"})
                continue
            # Live price from the WebSocket stream when it runs; a stale stream price blocks the entry.
            if self.stream is not None and getattr(self.stream, "connected", False):
                q = self.stream.quote(epic, self.quote_max_age)
                if q is None:
                    summary["skipped"].append({"epic": epic, "reason": f"Live-Kurs aelter als {self.quote_max_age:.0f} s"})
                    continue
                market = dict(market, bid=q["bid"], ask=q["ask"])

            is_demo = bool(getattr(self.broker, "is_demo", True))
            dec = self.gate.decide(epic, is_demo, self.mode, self.resolution)
            ok, strat, params, risk_factor, why = dec["ok"], dec["strategy"], dec["params"], dec["risk_factor"], dec["reason"]
            res = dec.get("resolution") or self.resolution
            if not ok:
                summary["skipped"].append({"epic": epic, "reason": f"keine Forschungsfreigabe: {why}"})
                continue
            stopped = self._stopped(epic, strat)
            if stopped:
                summary["skipped"].append({"epic": epic, "reason": stopped})
                continue
            if self.mode.upper() == "LIVE_MICRO":
                risk_factor *= 0.5          # erste Live-Stufe: halbes Risiko pro Trade (Skalierungs-Leiter)
                why = f"{why}; LIVE_MICRO x0.5"
            if self.overlay is not None:
                o_factor, o_why = self.overlay.factor(epic)
                if o_factor <= 0:
                    summary["skipped"].append({"epic": epic, "reason": f"Einstiege gesperrt ({o_why})"})
                    continue
                if o_factor < 1:
                    risk_factor *= o_factor
                    why = f"{why}; Risiko x{o_factor:.2f} ({o_why})"
            engine = self.engine or self._engine_for(strat, params, res)
            try:
                candles = await self.broker.get_candles(epic, res, self.lookback)
            except Exception as e:
                self._api_error()
                summary["skipped"].append({"epic": epic, "reason": f"candles unavailable: {e}"})
                continue
            stale = self._stale(candles, res)
            if stale:
                summary["skipped"].append({"epic": epic, "reason": stale})
                continue
            signal = engine.evaluate(epic, candles, market.get("bid"), market.get("ask"))
            if signal is None:
                summary["skipped"].append({"epic": epic, "reason": "no signal"})
                continue
            allowed, why_rules = self.rules.can_enter(epic, signal.side)
            if not allowed:
                summary["skipped"].append({"epic": epic, "reason": why_rules})
                self.event_store.append("TRADING_DESK", "SIGNAL_BLOCKED", {"epic": epic, "side": signal.side, "reason": why_rules})
                continue
            fx = await self._fx(market.get("currency"), account.get("currency"))
            if fx is None:
                summary["skipped"].append({"epic": epic, "reason": f"Wechselkurs {market.get('currency')}->{account.get('currency')} unbekannt"})
                continue
            risk_pct = float(self.limits["trade"]["max_risk_per_trade"])
            size, sizing_reason, _risk = size_position_v2(
                signal.entry, signal.stop_loss, balance, risk_pct, fx, market, risk_factor=risk_factor,
                max_leverage=float(self.limits["account"]["max_leverage"]))
            sizing_reason = f"[{strat}, {why}] {sizing_reason}"
            max_total = float(self.limits["trade"].get("max_total_risk", 0.015))
            if size > 0 and self._open_risk() + _risk > balance * max_total * 1.001:
                size = 0.0
                sizing_reason = (f"Gesamtrisiko offener Positionen {self._open_risk():.2f} + {_risk:.2f} "
                                 f"> {max_total:.1%} des Kontos (Klumpenrisiko)")
            if size <= 0:
                self.event_store.append(
                    "TRADING_DESK", "TRADE_REJECTED",
                    {"epic": epic, "side": signal.side, "price": signal.entry, "reason": sizing_reason, "stage": "sizing"},
                )
                self._archive("REJECTED", {
                    "epic": epic, "side": signal.side, "size": 0,
                    "price": signal.entry, "reason": sizing_reason, "stage": "sizing",
                })
                summary["skipped"].append({"epic": epic, "reason": sizing_reason})
                continue

            self.event_store.append(
                "TRADING_DESK", "SIGNAL",
                {
                    "epic": epic,
                    "side": signal.side,
                    "price": signal.entry,
                    "stop_loss": signal.stop_loss,
                    "take_profit": signal.take_profit,
                    "size": size,
                    "atr": signal.atr,
                    "strength": signal.strength,
                    "reason": signal.reason,
                    "sizing": sizing_reason,
                },
            )
            self._archive("SIGNAL", {
                "epic": epic, "side": signal.side, "size": size, "price": signal.entry,
                "stop_loss": signal.stop_loss, "take_profit": signal.take_profit,
                "reason": signal.reason, "sizing": sizing_reason,
                "atr": signal.atr, "strength": signal.strength,
            })

            proposal = TradeProposal(
                epic=epic,
                side=signal.side,
                size=size,
                stop_loss=round(signal.stop_loss, 6),
                take_profit=round(signal.take_profit, 6),
                reason=signal.reason,
            )

            result = await self.gateway.execute(
                proposal,
                account_balance=balance,
                current_positions=len(positions),
                current_price=signal.entry,
                fx_to_account=fx,
            )
            if result.get("status") == "ERROR":
                recovered = await self._recover_fill(epic, signal.side, size, known_deals, signal.reason)
                if recovered:
                    result = recovered

            if result.get("status") == "FILLED":
                self.state.mark_trade(epic, datetime.utcnow().timestamp())
                self.rules.on_entry()
                self.state.data.setdefault("pos_meta", {})[epic] = {
                    "side": signal.side, "strategy": strat, "params": params, "balance_at_entry": balance,
                    "risk": _risk, "lane": dec.get("lane") or why.split(" ")[0], "resolution": res,
                    "opened_ts": time.time()}
                self._save_rules()
                position_deal = await self._resolve_position_deal(epic, signal.side, known_deals)
                if position_deal:
                    known_deals.add(position_deal)
                positions.append({"epic": epic, "dealId": position_deal or result.get("order_id")})
                open_epics.add(epic)
                summary["trades"].append({
                    "epic": epic,
                    "side": signal.side,
                    "size": size,
                    "order_id": result.get("order_id"),
                    "position_id": position_deal,
                    "price": result.get("filled_price"),
                })
                self._archive("FILLED", {
                    "epic": epic,
                    "side": signal.side,
                    "size": result.get("filled_size") or size,
                    "price": result.get("filled_price"),
                    "deal_id": position_deal or result.get("order_id"),
                    "order_deal_id": result.get("order_id"),
                    "reason": signal.reason,
                    "sizing": sizing_reason,
                })
                logger.info(
                    f"TRADE {signal.side} {size} {epic} @ {result.get('filled_price')} "
                    f"({sizing_reason})"
                )
            else:
                self.event_store.append(
                    "TRADING_DESK", "TRADE_REJECTED",
                    {
                        "epic": epic,
                        "side": signal.side,
                        "size": size,
                        "reason": result.get("reason") or result.get("status"),
                        "stage": "gateway",
                    },
                )
                self._archive("REJECTED", {
                    "epic": epic, "side": signal.side, "size": size,
                    "price": signal.entry,
                    "reason": result.get("reason") or result.get("status"),
                    "stage": "gateway",
                })
                summary["skipped"].append({
                    "epic": epic,
                    "reason": result.get("reason") or result.get("status"),
                })

        if summary["trades"] or summary["closed"]:
            self.event_store.append(
                "TRADING_DESK", "DESK_CYCLE",
                {
                    "balance": balance,
                    "equity": equity,
                    "open_positions": len(positions),
                    "trades": summary["trades"],
                    "closed": summary["closed"],
                },
            )

        return summary

    async def _resolve_position_deal(
        self, epic: str, side: str, known_deals: set
    ) -> Optional[str]:
        """
        Capital.com's order confirmation returns the deal id of the opening
        deal, not the id of the resulting position. Closes are addressed by
        position id, so the archive must pair on that id. Look up the position
        that just appeared and use it; if the broker is slow or the answer is
        ambiguous, fall back to the order's deal id (the archive can still pair
        a close by epic, side and size).
        """
        try:
            opened = await self.broker.get_positions()
        except Exception as e:
            logger.debug(f"Could not resolve position id for {epic}: {e}")
            return None

        matches = [
            p for p in opened
            if str(p.get("epic") or "").upper() == epic.upper()
            and str(p.get("direction") or "").upper() == side.upper()
            and str(p.get("dealId") or "") not in known_deals
        ]
        if len(matches) == 1 and matches[0].get("dealId"):
            return str(matches[0]["dealId"])
        return None

    async def _manage_exits(self, positions: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
        """Close a position when the trend has flipped against it."""
        for position in positions:
            epic = str(position.get("epic") or "")
            deal_id = position.get("dealId")
            direction = str(position.get("direction") or "").upper()
            if not epic or not deal_id:
                continue

            meta = (self.state.data.get("pos_meta") or {}).get(epic) or {}
            strat = meta.get("strategy")
            if not strat:
                continue
            res = meta.get("resolution") or self.resolution
            try:
                candles = await self.broker.get_candles(epic, res, self.lookback)
            except Exception as e:
                self._api_error()
                logger.warning(f"Exit check failed for {epic}: {e}")
                continue
            # Dieselben Parameter und Kerzen wie beim Einstieg (vorher: Standardparameter -> andere Exit-Kanaele).
            engine = self.engine or self._engine_for(strat, meta.get("params"), res)
            if not engine.should_exit(epic, candles, direction, meta.get("opened_ts")):
                continue
            class _Reason:  # kleine Huelle, damit der Rest der Funktion unveraendert bleibt
                reason = f"{strat}: Ausstiegsregel ausgeloest"
            signal = _Reason()

            try:
                close_result = await self.broker.close_position(str(deal_id))
            except Exception as e:
                logger.error(f"Could not close {deal_id} ({epic}): {e}")
                continue

            close_result = close_result or {}
            exit_price = close_result.get("level") or close_result.get("price")
            profit = close_result.get("profit")
            profit_currency = close_result.get("profit_currency")

            self.state.mark_trade(epic, datetime.utcnow().timestamp())
            self.event_store.append(
                "TRADING_DESK", "POSITION_CLOSED",
                {
                    "epic": epic,
                    "deal_id": deal_id,
                    "direction": direction,
                    "size": position.get("volume"),
                    "price": exit_price,
                    "profit": profit,
                    "profit_currency": profit_currency,
                    "reason": f"exit rule: {signal.reason}",
                },
            )
            self._archive("CLOSED", {
                "epic": epic,
                "side": direction,
                "size": position.get("volume"),
                "price": exit_price,
                "profit": profit,
                "profit_currency": profit_currency,
                "deal_id": deal_id,
                "reason": f"trend flipped: {signal.reason}",
            })
            summary["closed"].append({"epic": epic, "deal_id": deal_id})
            logger.info(f"Closed {epic} {deal_id}: trend flipped")
