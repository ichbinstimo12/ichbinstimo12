"""Live-Kurse ueber den Capital.com-WebSocket (Money-Zone).

Protokoll laut open-api.capital.com (Stand 10/2026):
  URL      wss://api-streaming-capital.backend-capital.com/connect
  Abo      {"destination": "marketData.subscribe", "correlationId": "1", "cst": ..., "securityToken": ...,
            "payload": {"epics": [...]}}            (max. 40 Epics)
  Kurs     {"destination": "quote", "payload": {"epic", "bid", "ofr", "timestamp", ...}}
  Ping     {"destination": "ping", ...}             (mind. alle 10 Minuten, sonst endet die Sitzung)

Der Stream liefert nur Kurse. Er sendet nie Orders. Faellt er aus, nutzt der Desk die REST-Kurse; ist ein
Stream-Kurs zu alt, wird nicht gehandelt (Stale-Data-Check).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("PriceStream")

STREAM_URL = "wss://api-streaming-capital.backend-capital.com/connect"
PING_SECONDS = 300
MAX_EPICS = 40

SessionProvider = Callable[[], Awaitable[Tuple[str, str]]]


class PriceStream:
    def __init__(self, session_provider: SessionProvider, epics: List[str], url: str = STREAM_URL,
                 event_store: Any = None, now_fn=time.time):
        self.session_provider, self.url, self.event_store, self.now = session_provider, url, event_store, now_fn
        self.epics = list(dict.fromkeys(epics))[:MAX_EPICS]
        self.quotes: Dict[str, Dict[str, float]] = {}
        self.connected = False
        self.reconnects = 0
        self.last_error: Optional[str] = None
        self._running = False
        self._ws = None

    # ---------------------------------------------------------------- reading
    def quote(self, epic: str, max_age: float = 10.0) -> Optional[Dict[str, float]]:
        """Neuester Kurs, falls juenger als max_age Sekunden (Empfangszeit), sonst None."""
        q = self.quotes.get(epic)
        if not q or self.now() - q["received"] > max_age:
            return None
        return q

    def status(self) -> Dict[str, Any]:
        now = self.now()
        return {"connected": self.connected, "reconnects": self.reconnects, "last_error": self.last_error,
                "quote_age_s": {e: round(now - q["received"], 1) for e, q in self.quotes.items()},
                "quotes": {e: {"bid": q["bid"], "ask": q["ask"], "age_s": round(now - q["received"], 1)}
                           for e, q in self.quotes.items()}}

    # ---------------------------------------------------------------- protocol
    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        dest = msg.get("destination")
        p = msg.get("payload") or {}
        if dest == "quote" and p.get("epic"):
            try:
                bid, ask = float(p["bid"]), float(p["ofr"])
            except (KeyError, TypeError, ValueError):
                return
            ts = p.get("timestamp")
            self.quotes[p["epic"]] = {"bid": bid, "ask": ask, "ts": float(ts) / 1000.0 if ts else self.now(),
                                      "received": self.now()}
        elif dest == "marketData.subscribe" and msg.get("status") not in (None, "OK"):
            self.last_error = f"subscribe: {msg.get('status')} {str(p)[:200]}"
            logger.error(self.last_error)

    async def _send(self, ws, destination: str, payload: Optional[dict] = None, corr: str = "1") -> None:
        cst, token = await self.session_provider()
        msg = {"destination": destination, "correlationId": corr, "cst": cst, "securityToken": token}
        if payload is not None:
            msg["payload"] = payload
        await ws.send(json.dumps(msg))

    def _event(self, kind: str, payload: Dict[str, Any]) -> None:
        if self.event_store is not None:
            try:
                self.event_store.append("PRICE_STREAM", kind, payload)
            except Exception:
                pass

    async def _session(self) -> None:
        from websockets.asyncio.client import connect
        async with connect(self.url, open_timeout=15, ping_interval=None, close_timeout=5) as ws:
            self._ws = ws
            await self._send(ws, "marketData.subscribe", {"epics": self.epics})
            self.connected, self.last_error = True, None
            self._event("STREAM_CONNECTED", {"epics": self.epics})
            logger.info(f"Price stream connected: {', '.join(self.epics)}")
            last_ping = self.now()
            n = 1
            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    self._handle(raw)
                except asyncio.TimeoutError:
                    pass
                if self.now() - last_ping >= PING_SECONDS:
                    n += 1
                    await self._send(ws, "ping", corr=str(n))
                    last_ping = self.now()

    async def run(self) -> None:
        """Verbinden, abonnieren, empfangen; bei Fehlern mit wachsender Pause neu verbinden (max. 60 s)."""
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning(f"Price stream error: {self.last_error}")
            if self.connected:
                self._event("STREAM_DISCONNECTED", {"error": self.last_error})
            self.connected = False
            if not self._running:
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    async def set_epics(self, epics: List[str]) -> Dict[str, List[str]]:
        """Neue Abo-Liste (Reihenfolge = Prioritaet, max. 40; die Whitelist steht vorn). Laufende Verbindung:
        abbestellen, was wegfaellt, dann den Rest abonnieren - ohne Neuverbindung."""
        new = list(dict.fromkeys(epics))[:MAX_EPICS]
        removed = [e for e in self.epics if e not in new]
        added = [e for e in new if e not in self.epics]
        self.epics = new
        for e in removed:
            self.quotes.pop(e, None)
        ws = self._ws
        if ws is not None and self.connected:
            try:
                if removed:
                    await self._send(ws, "marketData.unsubscribe", {"epics": removed}, corr="unsub")
                if added:
                    await self._send(ws, "marketData.subscribe", {"epics": added}, corr="sub")
            except Exception as e:      # the reconnect loop subscribes the full new list
                self.last_error = f"resubscribe: {type(e).__name__}: {e}"
        return {"added": added, "removed": removed}

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
