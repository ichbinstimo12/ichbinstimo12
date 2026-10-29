"""Benachrichtigungen aufs Handy fuer den 24/7-Betrieb (eigene Zone, ohne Broker- und LLM-Schluessel).

Kanaele (beliebig kombinierbar, alles ueber .env):
  ntfy      NOTIFY_NTFY_URL=https://ntfy.sh/<geheimer-themenname>   (App "ntfy", optional NOTIFY_NTFY_TOKEN)
  Telegram  NOTIFY_TELEGRAM_BOT_TOKEN + NOTIFY_TELEGRAM_CHAT_ID
  Totmannschalter  HEALTHCHECK_PING_URL (z. B. healthchecks.io): alle 5 min ein Ping, solange alles laeuft;
            bei Stoerung ".../fail". Faellt der ganze Server aus, meldet der Dienst das von aussen.
Betraege (P&L, Kontostand) nur mit NOTIFY_INCLUDE_AMOUNTS=1 (ntfy-Themen sind fuer jeden lesbar, der den Namen kennt).
Gesendet wird nur, was im Event-Log steht; nichts wird erfunden. Rate-Limit gegen Nachrichtenfluten.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("Notifier")

HIGH = "high"
NORMAL = "default"
LOW = "low"


def _p(e) -> Dict[str, Any]:
    try:
        return json.loads(e.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}


def _agent_text(v, n: int = 160) -> str:
    """Agent-written text: no links, no control characters (a manipulated agent must not phish the operator)."""
    t = re.sub(r"https?://\S+|www\.\S+", "[Link entfernt]", str(v or ""))
    return re.sub(r"[\x00-\x1f\x7f]", " ", t)[:n]


def _num(v, fmt="{:+.2f}") -> str:
    return fmt.format(v) if isinstance(v, (int, float)) else "—"


def format_event(e: Dict[str, Any], amounts: bool) -> Optional[Tuple[str, str, str, str]]:
    """-> (titel, text, prioritaet, dedupe_schluessel) oder None, wenn das Event keine Nachricht wert ist."""
    t, p = e["type"], _p(e)
    if t == "ORDER_FILLED":
        detail = f" · {p.get('size')} @ {p.get('price')}" if amounts else ""
        return "Trade eroeffnet", f"{p.get('side')} {p.get('epic')}{detail}", NORMAL, f"fill:{e['id']}"
    if t == "TRADE_RESULT":
        r = p.get("r")
        res = f"{_num(p.get('pnl'))} ({_num(r, '{:+.2f}')} R)" if amounts else (
            "Gewinn" if (p.get("pnl") or 0) > 0 else "Verlust" if (p.get("pnl") or 0) < 0 else "neutral")
        return "Trade geschlossen", f"{p.get('epic')} {p.get('strategy') or ''}: {res}", NORMAL, f"res:{e['id']}"
    if t == "TRADING_HALTED":
        return "HANDEL GESTOPPT", " · ".join(str(x) for x in (p.get("reason"), p.get("action")) if x), HIGH, \
            f"halt:{p.get('reason')}"
    if t == "KILL_SWITCH":
        state = "alle Positionen geschlossen (vom Broker bestaetigt)" if p.get("flat") else \
            "GLATTSTELLEN NICHT BESTAETIGT - sofort im Broker pruefen!"
        return "KILL-SWITCH", f"{p.get('reason')}: {state}", HIGH, f"kill:{e['id']}"
    if t == "LLM_STATUS":
        if p.get("ok"):
            return "KI wieder verbunden", f"{p.get('provider')} {p.get('model') or ''}", LOW, f"llm:{e['id']}"
        return "KI getrennt", f"{p.get('provider')}: {str(p.get('error'))[:200]} - Agenten pausieren, Handel laeuft " \
            "deterministisch weiter", HIGH, "llm_down"
    if t == "LIMITS_WARNING":
        return "Limits widerspruechlich", str(p.get("warning"))[:300], HIGH, f"limits:{str(p.get('warning'))[:40]}"
    if t == "WHITELIST_CANDIDATE":
        return "Neuer Markt hat die Pruefung bestanden", f"{p.get('epic')}: {p.get('verdict')} mit {p.get('strategy')} " \
            f"({p.get('resolution')}). Zum Handeln in config/whitelist.toml eintragen - deine Entscheidung.", NORMAL, \
            f"wl:{p.get('epic')}"
    if t == "MEME_TRADE_OPEN":
        return "Memecoin gekauft", f"{p.get('symbol')} ({p.get('mode')}): {str(p.get('reason'))[:160]}" + \
            (f" · {p.get('size_sol')} SOL" if amounts else ""), NORMAL, f"meme_open:{e['id']}"
    if t in ("MEME_TRADE_CLOSE", "MEME_TRADE_PARTIAL"):
        res = f"{_num(p.get('pnl_pct'))} %" if p.get("pnl_pct") is not None else ""
        return ("Memecoin verkauft" if t == "MEME_TRADE_CLOSE" else "Memecoin Teilverkauf"), \
            f"{p.get('symbol')}: {p.get('reason')} {res}".strip(), NORMAL, f"meme_close:{e['id']}"
    if t == "MEME_LIMIT":
        return "MEMECOIN-DESK LIMIT", f"{p.get('limit')}: {p.get('action')}", HIGH, f"meme_limit:{p.get('limit')}"
    if t == "MEME_ERROR":
        return "Memecoin-Desk Fehler", str(p.get("error"))[:200], LOW, "meme_error"
    if t == "MEME_COACH_CHANGE":
        return "Memecoin-Coach: neue Parameter", f"Version {p.get('version')}: {str(p.get('diff'))[:200]}", NORMAL, \
            f"meme_coach:{p.get('version')}"
    if t == "ACCOUNT_BOUND":
        return "Konto erkannt", f"{p.get('account')} (Hoechststand/Tagesstart neu)", NORMAL, f"acct:{p.get('account')}"
    if t == "CIRCUIT_BREAKER":
        return "Schnellbremse", str(p.get("reason")), HIGH, "breaker"
    if t == "STRATEGY_STOPPED":
        return "Strategie gestoppt", f"{p.get('epic')} {p.get('strategy')}: {'; '.join(p.get('reasons') or [])}", HIGH, \
            f"stop:{p.get('epic')}:{p.get('strategy')}"
    if t == "BROKER_OFFLINE":
        return "Broker nicht erreichbar", str(p.get("error"))[:200], HIGH, "broker_offline"
    if t in ("RESEARCH_FAILED", "BACKUP_FAILED"):
        return ("Forschung fehlgeschlagen" if t == "RESEARCH_FAILED" else "Backup fehlgeschlagen"), \
            str(p.get("error"))[:200], HIGH, t
    if t == "INJECTION_SUSPECTED":
        return "Verdaechtige Daten blockiert", f"{p.get('agent')}: Aktionen verworfen", NORMAL, f"inj:{p.get('agent')}"
    if t == "STREAM_DISCONNECTED":
        return "Kursstream getrennt", str(p.get("error"))[:150], LOW, "stream"
    if t == "DESK_IDLE":
        why = "; ".join(f"{k}: {v}" for k, v in list((p.get("why_no_entry") or {}).items())[:3])
        return "Seit Stunden kein Einstieg", why[:300], LOW, "idle"
    if t == "RESEARCH_DONE":
        summ = "; ".join(f"{k}: {v}" for k, v in (p.get("summary") or {}).items())
        return "Forschung fertig", summ[:400], NORMAL, f"research:{e['id']}"
    if t == "DAILY_REPORT":
        return "Tagesbericht", f"{p.get('date')}: {p.get('fills')} Fills, {p.get('closed_trades')} geschlossen", LOW, \
            f"daily:{p.get('date')}"
    if t == "HIRING_REQUEST":
        return "Freigabe noetig: Einstellung", f"{p.get('role')} [Text vom Agenten, im Dashboard pruefen]: " \
            f"{_agent_text(p.get('justification'))}", NORMAL, f"hire:{p.get('request_id')}"
    if t == "PLAYBOOK_PROPOSED":
        return "Freigabe noetig: Playbook", f"{p.get('role')} [Text vom Agenten, im Dashboard pruefen]: " \
            f"{_agent_text(p.get('reason'))}", NORMAL, f"pb:{p.get('id')}"
    if t == "TASK_DIED":
        return "Hintergrundprozess abgestuerzt", f"{p.get('task')}: {str(p.get('error'))[:200]} - Neustart in " \
            f"{p.get('restart_in_s')} s", HIGH, f"died:{p.get('task')}"
    if t == "NEWS_DEGRADED":
        return "Wirtschaftskalender fehlt", f"{str(p.get('reason'))[:200]} - News-Sperre vor Terminen inaktiv", \
            NORMAL, "news_degraded"
    if t == "PROMOTION_CHECK" and p.get("ready"):
        return "Skalierungs-Leiter: Kriterien erfuellt", \
            f"{p.get('stage')} -> {p.get('next')} waere begruendet. Entscheidung liegt bei dir.", NORMAL, "promo"
    return None


NOTIFY_TYPES = ["ORDER_FILLED", "TRADE_RESULT", "TRADING_HALTED", "KILL_SWITCH", "LLM_STATUS", "ACCOUNT_BOUND", "LIMITS_WARNING", "WHITELIST_CANDIDATE", "TASK_DIED", "NEWS_DEGRADED", "MEME_TRADE_OPEN",
                "MEME_TRADE_CLOSE", "MEME_TRADE_PARTIAL", "MEME_LIMIT", "MEME_ERROR", "MEME_COACH_CHANGE", "CIRCUIT_BREAKER", "STRATEGY_STOPPED",
                "BROKER_OFFLINE", "RESEARCH_FAILED", "BACKUP_FAILED", "INJECTION_SUSPECTED", "STREAM_DISCONNECTED",
                "DESK_IDLE", "RESEARCH_DONE", "DAILY_REPORT", "HIRING_REQUEST", "PLAYBOOK_PROPOSED", "PROMOTION_CHECK"]


class Notifier:
    def __init__(self, env: Optional[Dict[str, str]] = None, client: Optional[httpx.AsyncClient] = None,
                 now_fn=time.time, max_per_hour: int = 30, dedupe_s: float = 600):
        e = env if env is not None else os.environ
        self.ntfy_url, self.ntfy_token = e.get("NOTIFY_NTFY_URL") or "", e.get("NOTIFY_NTFY_TOKEN") or ""
        self.tg_token, self.tg_chat = e.get("NOTIFY_TELEGRAM_BOT_TOKEN") or "", e.get("NOTIFY_TELEGRAM_CHAT_ID") or ""
        self.hc_url = e.get("HEALTHCHECK_PING_URL") or ""
        self.amounts = (e.get("NOTIFY_INCLUDE_AMOUNTS") or "0") == "1"
        self.client = client or httpx.AsyncClient(timeout=15)
        self.now, self.max_per_hour, self.dedupe_s = now_fn, max_per_hour, dedupe_s
        self._sent: List[float] = []
        cf = e.get("NOTIFY_CURSOR_FILE", "data/notify_cursor.txt") if env is None else e.get("NOTIFY_CURSOR_FILE")
        self.cursor_file: Optional[Path] = Path(cf) if cf else None
        self._last_key: Dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self.ntfy_url or (self.tg_token and self.tg_chat) or self.hc_url)

    def _allowed(self, key: str, priority: str) -> bool:
        now = self.now()
        self._sent = [t for t in self._sent if now - t < 3600]
        if now - self._last_key.get(key, -1e12) < self.dedupe_s:
            return False
        if len(self._sent) >= self.max_per_hour and priority != HIGH:
            return False
        return True

    def _mark(self, key: str) -> None:
        """Only a delivered message counts for dedupe/rate limit (a failed alarm is retried)."""
        now = self.now()
        self._last_key[key] = now
        self._sent.append(now)

    async def send(self, title: str, text: str, priority: str = NORMAL) -> bool:
        ok = False
        if self.ntfy_url:
            headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority, "Tags": "chart_with_upwards_trend"}
            if self.ntfy_token:
                headers["Authorization"] = f"Bearer {self.ntfy_token}"
            try:
                r = await self.client.post(self.ntfy_url, content=text.encode("utf-8"), headers=headers)
                ok = ok or r.status_code < 300
                if r.status_code >= 300:
                    logger.warning(f"ntfy rejected the message: HTTP {r.status_code}")
            except Exception as ex:
                logger.warning(f"ntfy failed: {type(ex).__name__}")
        if self.tg_token and self.tg_chat:
            try:
                r = await self.client.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                                           json={"chat_id": self.tg_chat, "text": f"{title}\n{text}",
                                                 "disable_notification": priority == LOW})
                ok = ok or r.status_code < 300
                if r.status_code >= 300:
                    logger.warning(f"telegram rejected the message: HTTP {r.status_code}")
            except Exception as ex:
                logger.warning(f"telegram failed: {type(ex).__name__}")   # never log the URL (contains the token)
        return ok

    async def handle(self, event: Dict[str, Any]) -> bool:
        msg = format_event(event, self.amounts)
        if msg is None:
            return False
        title, text, prio, key = msg
        if not self._allowed(key, prio):
            return False
        ok = await self.send(title, text, prio)
        if not ok and prio == HIGH:
            await asyncio.sleep(2)
            ok = await self.send(title, text, prio)      # alarms get a second attempt
        if ok:
            self._mark(key)
        return ok

    async def ping(self, healthy: bool) -> None:
        if not self.hc_url:
            return
        try:
            await self.client.get(self.hc_url if healthy else self.hc_url.rstrip("/") + "/fail")
        except Exception as ex:
            logger.warning(f"healthcheck ping failed: {type(ex).__name__}")

    async def _drain(self, event_store, cursor: str) -> str:
        for e in event_store.get_events(after_ts=cursor, types=NOTIFY_TYPES):
            cursor = e["ts"]                       # always advance: one bad event must not block later alarms
            try:
                await self.handle(e)
            except Exception as ex:
                logger.error(f"notification for {e.get('type')} failed: {type(ex).__name__}: {ex}")
        self._save_cursor(cursor)
        return cursor

    def _load_cursor(self) -> str:
        """Continue after the last notified event (events during a restart of this zone are not lost);
        at most one hour back."""
        floor = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        try:
            saved = self.cursor_file.read_text(encoding="utf-8").strip() if self.cursor_file else ""
        except OSError:
            saved = ""
        if saved:
            return max(saved, floor)
        return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    def _save_cursor(self, cursor: str) -> None:
        if not self.cursor_file:
            return
        try:
            self.cursor_file.parent.mkdir(parents=True, exist_ok=True)
            self.cursor_file.write_text(cursor, encoding="utf-8")
        except OSError as ex:
            logger.warning(f"notify cursor not saved: {ex}")

    async def run(self, event_store, halt_file: Path, poll_s: float = 15, ping_s: float = 300,
                  stop: Optional[asyncio.Event] = None) -> None:
        """Bis `stop` gesetzt ist: neue Events melden, regelmaessig den Totmannschalter pingen.
        Beim Stoppen werden die letzten Events noch gemeldet (z. B. Halt + Glattstellung)."""
        from src.firm.ops.health import zone_health
        stop = stop or asyncio.Event()
        cursor = self._load_cursor()
        last_ping = -1e12
        chat = bool(self.ntfy_url or (self.tg_token and self.tg_chat))
        if chat:
            await self.send("Firma gestartet", "Benachrichtigungen aktiv.", LOW)
        while not stop.is_set():
            try:
                cursor = await self._drain(event_store, cursor)
                if self.now() - last_ping >= ping_s:
                    await self.ping(zone_health(event_store, halt_file)["ok"])
                    last_ping = self.now()
            except Exception as ex:
                logger.error(f"Notifier loop error: {type(ex).__name__}: {ex}")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_s)
        try:
            await self._drain(event_store, cursor)
        except Exception as ex:
            logger.error(f"Notifier final drain failed: {type(ex).__name__}")
        if chat:
            await self.send("Firma gestoppt", "Prozesse beendet." + (" HALT aktiv." if halt_file.exists() else ""), HIGH)
        await self.ping(False)

    async def close(self) -> None:
        await self.client.aclose()
