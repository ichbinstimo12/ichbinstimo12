"""Memecoin-Desk (Solana): eigenstaendiger Desk mit sieben Agenten, eigener Prozess-Zone und eigenem Kapital.

  Scout             neue/trendende Tokens (DexScreener), Grobfilter (DEX, Liquiditaet, Alter, Volumen)
  NarrativeAnalyst  Aufmerksamkeit + Frische (Boosts, Socials, X optional, Nachrichten, Kopie-Wellen)
  WalletTracker     Smart-Money-/KOL-Wallets (config/memedesk_wallets.toml): fruehe Kaeufe, Verkaeufe
  SafetyChecker     VETO: Mint/Freeze-Authority, LP, Holder-Konzentration, Insider/Bundles, Honeypot, Liquiditaet
  RiskManager       VETO: Einsatz 1-2 %, max. 5 Positionen, Tagesverlust -5 % (Pause), Drawdown -20 % (Stop)
  Executor          PAPER-Fills mit echten Jupiter-Quotes, Slippage, Gebuehren; LIVE gesperrt
  JournalCoach      jeder Trade mit Grund und Daten-Schnappschuss; Coach passt nur Gewichte/Schwellen an
Jede Entscheidung (auch jedes Veto) wird als Ereignis MEME_* mit Agent und Begruendung protokolliert.
Kein LLM entscheidet hier ueber Geld; der Desk kennt keine Broker-Schluessel der Firma und kein Haupt-Wallet.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.firm.agents.guard import screen
from src.firm.memedesk import logic, paper

logger = logging.getLogger("MemeDesk")

DATA = Path("data/memedesk")
SAFETY_TTL = 300
VETO_TTL = 600


def _today(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


class MemeDesk:
    def __init__(self, sources, event_store, cfg: Optional[Dict[str, Any]] = None,
                 limits: Optional[Dict[str, Any]] = None, data_dir: Path = DATA, journal_dir: Path = Path("archiv/memedesk"),
                 wallets: Optional[List[Dict[str, str]]] = None, now_fn=time.time):
        self.src, self.store, self.now = sources, event_store, now_fn
        self.data_dir, self.journal_dir = Path(data_dir), Path(journal_dir)
        self.cfg = cfg or logic.load_config(override=self.data_dir / "params_current.json")
        self.limits = limits or logic.load_limits()
        self.wallets = wallets if wallets is not None else self._load_wallets()
        self.state = self._load_state()
        self.watchlist: Dict[str, Dict[str, Any]] = {}
        self._safety_cache: Dict[str, Any] = {}
        self._candle_cache: Dict[str, Any] = {}
        self._error_at: Dict[str, float] = {}
        self._veto_until: Dict[str, float] = {}
        self._holders: Dict[str, List[tuple]] = {}
        self._wallet_cursor: Dict[str, str] = {}
        self._wallet_buys: Dict[str, Dict[str, float]] = {}     # mint -> {wallet: ts}
        self._wallet_sells: Dict[str, set] = {}
        self._recent_names: List[str] = []
        self._last_watch_event = 0.0

    # ------------------------------------------------------------------ persistence
    def _state_path(self) -> Path:
        return self.data_dir / "state.json"

    def _load_state(self) -> Dict[str, Any]:
        p = self._state_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except ValueError as e:
                logger.critical(f"memedesk state unreadable ({e}) - starting a fresh PAPER account")
        start = float(self.limits["capital"]["paper_start_sol"])
        return {"mode": "PAPER", "equity": start, "equity_mark": start, "peak": start, "positions": {},
                "created": self.now(), "closed_trades": 0, "halted": None, "paused_day": None}

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, default=str), encoding="utf-8")
        tmp.replace(self._state_path())

    def _load_wallets(self) -> List[Dict[str, str]]:
        try:
            import tomllib
            return list(tomllib.loads(Path("config/memedesk_wallets.toml").read_text(encoding="utf-8")).get("wallets") or [])
        except (OSError, ValueError):
            return []

    def log(self, agent: str, kind: str, payload: Dict[str, Any]) -> None:
        self.store.append(f"MEME_{agent}", kind, payload)

    def journal(self, row: Dict[str, Any]) -> None:
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        with (self.journal_dir / "journal.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    # ------------------------------------------------------------------ wallet tracker
    async def track_wallets(self) -> None:
        window = 30 * 60
        for w in self.wallets:
            addr = w.get("address")
            if not addr:
                continue
            try:
                swaps = await self.src.wallet_swaps(addr, until_sig=self._wallet_cursor.get(addr))
            except Exception as e:
                self.log("WALLETTRACKER", "MEME_ERROR", {"wallet": w.get("label") or addr[:6], "error": str(e)[:200]})
                continue
            if swaps:
                self._wallet_cursor[addr] = swaps[0]["signature"]
            for s in swaps:
                if s["side"] == "BUY":
                    self._wallet_buys.setdefault(s["mint"], {})[addr] = float(s.get("ts") or self.now())
                else:
                    self._wallet_sells.setdefault(s["mint"], set()).add(addr)
                self.log("WALLETTRACKER", "MEME_WALLET_SWAP", {"wallet": w.get("label") or addr[:6], "mint": s["mint"],
                                                                "side": s["side"]})
        cutoff = self.now() - window
        for mint in list(self._wallet_buys):
            self._wallet_buys[mint] = {a: t for a, t in self._wallet_buys[mint].items() if t >= cutoff}

    def smart_buyers(self, mint: str) -> int:
        return len(self._wallet_buys.get(mint) or {})

    # ------------------------------------------------------------------ scout -> decide -> buy
    async def scan(self) -> List[Dict[str, Any]]:
        if self.state.get("halted") or self._killed():
            return []
        u, loop = self.cfg["universe"], self.cfg["loop"]
        tokens = await self.src.trending_tokens()
        prelim, rejected = [], {}
        for tok in tokens[: int(loop["max_candidates"])]:
            mint = tok["mint"]
            if mint in self.state["positions"] or self._veto_until.get(mint, 0) > self.now():
                continue
            try:
                pair = await self.src.pair(mint)
            except Exception as e:
                self.log("SCOUT", "MEME_ERROR", {"mint": mint, "error": str(e)[:200]})
                continue
            if not pair:
                continue
            age_h = (self.now() * 1000 - float(pair.get("created_ms") or 0)) / 3.6e6
            reject = None
            if pair["dex"] not in u["dexes"]:
                reject = f"DEX {pair['dex']} nicht im Universum"
            elif pair["liquidity_usd"] < u["min_liquidity_usd"]:
                reject = f"Liquiditaet {pair['liquidity_usd']:,.0f} USD"
            elif age_h > u["max_age_hours"]:
                reject = f"aelter als {u['max_age_hours']} h"
            elif pair["volume"]["h1"] < u["min_volume_h1_usd"]:
                reject = f"Volumen 1h {pair['volume']['h1']:,.0f} USD"
            name, _ = screen(f"{pair.get('symbol') or ''} {pair.get('name') or ''}", limit=60)
            self._recent_names = (self._recent_names + [name])[-300:]
            if reject:
                self._veto_until[mint] = self.now() + VETO_TTL
                key = reject.split(" ")[0] if not reject.startswith("DEX") else "DEX"
                rejected[key] = rejected.get(key, 0) + 1
                continue
            narrative, why = logic.narrative_score(tok, pair, self._recent_names, now_ms=self.now() * 1000)
            prelim.append((narrative, tok, pair, why))
        prelim.sort(key=lambda r: r[0], reverse=True)
        self.log("SCOUT", "MEME_SCAN", {"seen": len(tokens), "checked": min(len(tokens), int(loop["max_candidates"])),
                                        "passed_filter": len(prelim), "rejected": rejected,
                                        "deep_checks": min(len(prelim), int(loop.get("deep_checks", 6)))})
        decisions = []
        for narrative, tok, pair, why in prelim[: int(loop.get("deep_checks", 6))]:
            decisions.append(await self._deep_check(tok, pair, narrative, why))
        self._publish_watchlist()
        self.save()
        return decisions

    async def _safety(self, mint: str) -> tuple:
        c = self._safety_cache.get(mint)
        if c and c[0] > self.now():
            return c[1], c[2]
        try:
            report = await self.src.safety_report(mint)
        except Exception:
            report = None
        try:
            loss, _ = await self.src.roundtrip_loss_pct(mint, 0.1)
        except Exception:
            loss = None
        ttl = SAFETY_TTL if report is not None and loss is not None else 30    # missing data: retry soon
        self._safety_cache[mint] = (self.now() + ttl, report, loss)
        return report, loss

    async def _deep_check(self, tok, pair, narrative, narrative_why) -> Dict[str, Any]:
        mint = pair["mint"]
        report, loss = await self._safety(mint)
        vetoes = logic.safety_veto(report, loss, pair, self.cfg)
        holders = (report or {}).get("total_holders")
        hist = self._holders.setdefault(mint, [])
        if holders:
            hist.append((self.now(), holders))
            del hist[:-20]
        growth = (hist[-1][1] / hist[0][1] - 1) if len(hist) >= 2 and hist[0][1] else None
        cached = self._candle_cache.get(pair["pair"])
        if cached and cached[0] > self.now():
            candles = cached[1]
        else:
            try:
                candles = await self.src.candles(pair["pair"], 5, 48)
                self._candle_cache[pair["pair"]] = (self.now() + 60, candles)
            except Exception:
                candles = []
        smart = self.smart_buyers(mint) if self.wallets else None      # None = no wallets configured (n/a)
        ev = logic.evaluate_signals(pair, vetoes, report, narrative, growth, smart, candles, self.cfg)
        entry = {"mint": mint, "symbol": pair.get("symbol"), "dex": pair.get("dex"), "score": ev["score"],
                 "signals": ev["signals"], "count": ev["count"], "vetoes": vetoes, "chasing": ev["chasing"],
                 "structure": ev["structure"], "narrative_why": narrative_why[:4], "price_usd": pair.get("price_usd"),
                 "liquidity_usd": pair.get("liquidity_usd"), "ts": self.now()}
        self.watchlist[mint] = entry
        self.log("NARRATIVE", "MEME_NARRATIVE", {"mint": mint, "symbol": pair.get("symbol"), "score": narrative,
                                                 "why": narrative_why[:4]})
        if vetoes:
            if not all(v.startswith(logic.DATA_MISSING) for v in vetoes):
                self._veto_until[mint] = self.now() + VETO_TTL     # real safety problem: leave it alone a while
            self.log("SAFETY", "MEME_VETO", {"mint": mint, "symbol": pair.get("symbol"), "reasons": vetoes})
            return {**entry, "decision": "veto_safety"}
        if not ev["enter"]:
            why = "senkrechter Pump - warte auf Pullback/Ausbruch" if ev["chasing"] else \
                f"nur {ev['count']} von 5 Signalen (mind. {int(self.cfg['thresholds']['min_signals'])})" \
                if ev["count"] < int(self.cfg["thresholds"]["min_signals"]) else \
                "kein Geldfluss (weder Volumen/Kaeufer noch Smart Money) - Aufmerksamkeit allein reicht nicht"
            self.log("SCOUT", "MEME_SKIP", {"mint": mint, "symbol": pair.get("symbol"), "reason": why,
                                            "signals": ev["signals"]})
            return {**entry, "decision": "skip", "reason": why}
        veto, size = logic.risk_check(self.state, self.limits, _today(self.now()))
        if veto:
            self.log("RISK", "MEME_VETO", {"mint": mint, "symbol": pair.get("symbol"), "reasons": [veto]})
            return {**entry, "decision": "veto_risk", "reason": veto}
        return await self._buy(pair, report, ev, candles, size, entry)

    async def _buy(self, pair, report, ev, candles, size, entry) -> Dict[str, Any]:
        mint = pair["mint"]
        if self.state.get("mode") != "PAPER":
            paper.LiveExecutor()       # raises NotImplementedError: LIVE is not built/unlocked
        try:
            fill = await paper.paper_buy(self.src, mint, size, self.cfg)
        except Exception as e:
            self.log("EXECUTOR", "MEME_ERROR", {"mint": mint, "error": str(e)[:200]})
            return {**entry, "decision": "error", "reason": str(e)[:200]}
        t = self.now()
        self.state["equity"] = round(float(self.state["equity"]) - fill["cost_sol"], 9)
        self.state["positions"][mint] = {
            "mint": mint, "symbol": pair.get("symbol"), "pair": pair.get("pair"), "cost_sol": fill["cost_sol"],
            "tokens_raw": fill["tokens_raw"], "opened_at": t, "peak_value": fill["cost_sol"], "free_ride": False,
            "tiers_done": [], "realized_sol": 0.0, "last_move_at": t, "last_move_value": fill["cost_sol"],
            "creator_balance": (report or {}).get("creator_balance"), "liquidity_at_entry": pair.get("liquidity_usd"),
            "exit_cost_pct": round(fill["slippage_bps"] / 200 + float(fill["impact_pct"] or 0), 3),
            "decimals": (report or {}).get("decimals"),
            "metrics": ev["metrics"], "signals": ev["signals"], "structure_ok": ev["signals"]["structure"],
            "distribution_ok": ev["signals"]["distribution"], "holders_up": ev["signals"]["volume_holders"],
        }
        snapshot = {"pair": pair, "safety": report, "signals": ev, "candles": candles[-12:], "fill": fill}
        row = {"type": "OPEN", "ts": t, "mint": mint, "symbol": pair.get("symbol"), "mode": self.state["mode"],
               "size_sol": fill["cost_sol"], "reason": f"{ev['count']}/5 Signale: " +
               ", ".join(k for k, v in ev["signals"].items() if v) + f"; {ev['structure']}", "snapshot": snapshot}
        self.journal(row)
        self.log("EXECUTOR", "MEME_TRADE_OPEN", {k: row[k] for k in ("mint", "symbol", "mode", "size_sol", "reason")}
                 | {"slippage_bps": fill["slippage_bps"], "impact_pct": fill["impact_pct"]})
        self.save()
        return {**entry, "decision": "buy", "size_sol": fill["cost_sol"]}

    # ------------------------------------------------------------------ manage open positions
    async def manage(self) -> List[Dict[str, Any]]:
        closed = []
        # Kill-Switch or a full stop (drawdown -20 %): sell everything, not just stop buying
        killed = self._killed() or bool(self.state.get("halted"))
        marked = float(self.state["equity"])
        mints = list(self.state["positions"])
        native, prices = {}, {}
        if mints:
            try:
                native = await self.src.native_prices(mints)          # DexScreener: price in SOL, generous limit
            except Exception as e:
                self._error_once("prices_dex", f"DexScreener-Preise: {str(e)[:150]}")
            if any(m not in native for m in mints):
                try:
                    prices = await self.src.prices(mints + [paper.SOL_MINT])   # fallback: Jupiter USD prices
                except Exception as e:
                    self._error_once("prices_jup", f"Jupiter-Preise: {str(e)[:150]}")
        for mint, pos in list(self.state["positions"].items()):
            value = paper.estimate_value(pos, prices, self.cfg, native)
            if value is None:       # no price: keep the last value, retry next cycle (said once a minute)
                self._error_once(f"noprice:{mint}", f"kein Preis fuer {pos.get('symbol') or mint[:6]} - Stops "
                                                    "pausieren bis ein Preis kommt", mint=mint)
                marked += float(pos.get("last_value") or pos["cost_sol"])
                continue
            pos["last_value"] = value
            paper.mark(pos, value, self.now(), self.cfg["exits"]["time_stop_band_pct"])
            alerts = [f"Desk-Stopp ({self.state.get('halted') or 'Kill-Switch'})"] if killed else await self._alerts(mint, pos)
            dec = paper.exit_decision(pos, value, self.now(), self.cfg, self.limits, alerts)
            if dec:
                reason, frac = dec
                try:      # the real (paper) sale uses a Jupiter quote for exactly this amount
                    fill = await paper.paper_sell_value(self.src, mint, float(pos["tokens_raw"]) * frac, self.cfg)
                    sold = fill["sol"]
                except Exception as e:
                    self.log("EXECUTOR", "MEME_ERROR", {"mint": mint, "error": f"Verkauf ({reason}) verschoben: "
                                                                               f"{str(e)[:150]}"})
                    marked += value
                    continue
                done = paper.apply_exit(pos, reason, frac, sold)
                self.state["equity"] = round(float(self.state["equity"]) + sold, 9)
                kind = "MEME_TRADE_CLOSE" if done else "MEME_TRADE_PARTIAL"
                pnl = float(pos["realized_sol"]) - float(pos["cost_sol"]) if done else None
                row = {"type": "CLOSE" if done else "PARTIAL", "ts": self.now(), "mint": mint, "symbol": pos.get("symbol"),
                       "reason": reason, "sold_sol": round(sold, 9), "fraction": round(frac, 4),
                       "pnl_sol": None if pnl is None else round(pnl, 9),
                       "pnl_pct": None if pnl is None else round(pnl / pos["cost_sol"] * 100, 2),
                       "minutes": round((self.now() - pos["opened_at"]) / 60, 1),
                       "metrics": pos.get("metrics"), "signals": pos.get("signals"), "mode": self.state["mode"]}
                self.journal(row)
                self.log("EXECUTOR", kind, {k: v for k, v in row.items() if k not in ("metrics",)})
                if done:
                    self.state["positions"].pop(mint, None)
                    self.state["closed_trades"] = int(self.state.get("closed_trades") or 0) + 1
                    closed.append(row)
                    continue
                value *= (1 - frac)
            marked += value
        self.state["equity_mark"] = round(marked, 9)
        event = logic.update_limits(self.state, self.limits, _today(self.now()))
        if event == "pause":
            self.log("RISK", "MEME_LIMIT", {"limit": "daily_loss", "action": "Pause bis morgen (UTC)",
                                            "equity": self.state["equity_mark"], "day_start": self.state.get("day_start")})
        elif event == "halt":
            self.log("RISK", "MEME_LIMIT", {"limit": "drawdown", "action": "Desk gestoppt - Wiederanlauf nur durch dich",
                                            "equity": self.state["equity_mark"], "peak": self.state.get("peak")})
        if killed and not self.state["positions"] and not self.state.get("halted"):
            self.state["halted"] = "Kill-Switch"
            self.log("RISK", "MEME_LIMIT", {"limit": "kill_switch", "action": "alle Positionen geschlossen, Desk gestoppt"})
        self.save()
        return closed

    async def _alerts(self, mint: str, pos: Dict[str, Any]) -> List[str]:
        """Sofort-raus-Gruende: Smart-Money-Exit, Dev-Verkauf, Narrativ-/Liquiditaets-Bruch, Sicherheitsproblem."""
        alerts = []
        sellers = self._wallet_sells.get(mint, set()) & set(self._wallet_buys.get(mint, {}))
        if sellers:
            alerts.append(f"Smart-Money-Exit ({len(sellers)} Wallet(s))")
        if self.now() - float(pos.get("checked_at") or 0) >= 120:
            pos["checked_at"] = self.now()
            try:
                rep = await self.src.safety_report(mint)
                cb0, cb1 = pos.get("creator_balance"), rep.get("creator_balance")
                if isinstance(cb0, (int, float)) and isinstance(cb1, (int, float)) and cb0 > 0 and cb1 < cb0 * 0.8:
                    alerts.append("Dev verkauft")
                if rep.get("mint_authority") or rep.get("freeze_authority") or rep.get("rugged"):
                    alerts.append("Sicherheitsproblem (Authority/Rug)")
            except Exception:
                pass
            try:
                p = await self.src.pair(mint)
                if p and pos.get("liquidity_at_entry") and p["liquidity_usd"] < 0.5 * pos["liquidity_at_entry"]:
                    alerts.append("Liquiditaet halbiert (Narrativ-Bruch)")
                if p and p["sells_m5"] > 3 * max(1, p["buys_m5"]) and p["change"]["m5"] < -15:
                    alerts.append("Abverkauf (Narrativ-Bruch)")
            except Exception:
                pass
        return alerts

    def _error_once(self, key: str, error: str, every: float = 60.0, **extra) -> None:
        """Error event at most once per `every` seconds per key (no log flooding during an API outage)."""
        if self.now() - self._error_at.get(key, -1e12) >= every:
            self._error_at[key] = self.now()
            self.log("EXECUTOR", "MEME_ERROR", {"error": error, **extra})

    # ------------------------------------------------------------------ kill switch, watchlist, status
    def _killed(self) -> bool:
        return (self.data_dir / "halt").exists()

    def _publish_watchlist(self) -> None:
        if self.now() - self._last_watch_event < 60:
            return
        self._last_watch_event = self.now()
        cutoff = self.now() - 1800
        rows = sorted((w for w in self.watchlist.values() if w["ts"] >= cutoff), key=lambda w: w["score"], reverse=True)
        self.log("SCOUT", "MEME_WATCHLIST", {"tokens": rows[:15]})

    def status(self) -> Dict[str, Any]:
        return {k: self.state.get(k) for k in ("mode", "equity", "equity_mark", "peak", "day_start", "paused_day",
                                               "halted", "closed_trades", "created")} | {
            "positions": list(self.state["positions"].values()), "wallets_tracked": len(self.wallets)}

    async def _every(self, name: str, seconds: float, fn, is_running) -> None:
        while is_running():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"memedesk {name} error: {e}")
                self.log("DESK", "MEME_ERROR", {"loop": name, "error": str(e)[:300]})
            await asyncio.sleep(seconds)

    async def run(self, is_running=lambda: True) -> None:
        """Scout, Positionsverwaltung und Wallet-Tracker laufen parallel: eine lange Scan-Runde (Rate-Limits der
        Datenquellen) verzoegert nie die Stop-Pruefung offener Positionen."""
        loop = self.cfg["loop"]
        logger.info(f"Memecoin desk started ({self.state['mode']}, {self.state['equity']:.3f} SOL, "
                    f"{len(self.wallets)} wallets).")

        async def manage_if_open():
            if self.state["positions"] or self._killed() or self.state.get("halted"):
                await self.manage()
        tasks = [asyncio.create_task(self._every("scout", float(loop["scan_seconds"]), self.scan, is_running)),
                 asyncio.create_task(self._every("manage", float(loop["manage_seconds"]), manage_if_open, is_running))]
        if self.wallets:
            tasks.append(asyncio.create_task(self._every("wallets", 30.0, self.track_wallets, is_running)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.save()
