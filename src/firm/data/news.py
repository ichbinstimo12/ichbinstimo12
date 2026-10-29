"""News-Ticker und Wirtschaftskalender (Brain-Zone: kein Broker-Schluessel, nur lesende HTTP-Abrufe).

Ablauf: alle `poll_seconds` (Standard 5 s) wird geprueft, welche Quelle faellig ist. Jede Quelle wird hoechstens
alle `min_interval_s` abgerufen, mit ETag/If-Modified-Since (unveraendert -> 304, kaum Datenverkehr), und bei
429/5xx mit wachsender Pause (Retry-After wird beachtet). Ein schnellerer Abruf bringt keine frueheren Meldungen:
RSS-Feeds werden von den Anbietern selbst nur alle paar Minuten aktualisiert.

Ereignisse:
  NEWS_ITEM      jede neue Meldung (Quelle, Titel, Link, Zeit, betroffene Maerkte, tainted)
  NEWS_ALERT     neue, unbelastete Meldung, die einen Whitelist-Markt betrifft -> weckt Analysten
  ECON_CALENDAR  High-Impact-Termine der Woche (bei Aenderung) -> Einstiegssperre im Desk (nur Risiko-Reduktion)
Fremder Text ist Daten, nie Anweisung: Titel/Zusammenfassung laufen durch den Injection-Schutz; belastete Meldungen
werden geschwaerzt gespeichert und wecken keinen Agenten.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import tomllib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from src.firm.agents.guard import screen

logger = logging.getLogger("NewsWire")

MAX_BYTES = 2_000_000
USER_AGENT = "TradingFirmNewsWire/1.0 (+polite polling, ETag)"
MAX_SEEN = 20_000
FRESH_HOURS = 3          # older headlines are stored but never alert anybody (first fetch, restarts, slow feeds)


def safe_link(link: str) -> str:
    """Nur http(s)-Links ohne Leerzeichen; alles andere wird verworfen."""
    link = (link or "").strip()[:500]
    u = urlparse(link)
    return link if u.scheme in ("http", "https") and u.netloc and not re.search(r"\s", link) else ""


def load_config(path: str | Path = "config/news.toml") -> Dict[str, Any]:
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"news config unavailable: {e}")
        return {}


def _text(el: Optional[ET.Element]) -> str:
    return (el.text or "").strip() if el is not None else ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ts(raw: str) -> Optional[str]:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_feed(xml_text: str) -> List[Dict[str, Any]]:
    """RSS 2.0 und Atom -> [{guid, title, link, published, summary}]."""
    root = ET.fromstring(xml_text)
    out = []
    for el in root.iter():
        name = _local(el.tag)
        if name not in ("item", "entry"):
            continue
        fields = {}
        for c in el:
            fields.setdefault(_local(c.tag), c)          # first occurrence wins
        link = _text(fields.get("link"))
        if not link:                                     # Atom: prefer rel="alternate" (or no rel)
            links = [c for c in el if _local(c.tag) == "link"]
            pick = next((c for c in links if c.get("rel") in (None, "alternate")), links[0] if links else None)
            link = pick.get("href", "") if pick is not None else ""
        title = _text(fields.get("title"))
        guid = _text(fields.get("guid")) or _text(fields.get("id")) or link or title
        published = _ts(_text(fields.get("pubDate")) or _text(fields.get("published")) or _text(fields.get("updated")))
        summary = re.sub(r"<[^>]+>", " ", _text(fields.get("description")) or _text(fields.get("summary")))
        if title or link:
            out.append({"guid": guid, "title": title, "link": link, "published": published,
                        "summary": re.sub(r"\s+", " ", summary).strip()[:300]})
    return out


def blackout_for(epic: str, events: List[Dict[str, Any]], now: float, currencies: Dict[str, List[str]],
                 before_min: float, after_min: float) -> Optional[str]:
    """Grund fuer eine Einstiegssperre, wenn ein High-Impact-Termin einer betroffenen Waehrung nahe ist."""
    ccys = set(currencies.get(epic) or [])
    if not ccys:
        return None
    for ev in events:
        if ev.get("country") not in ccys or not ev.get("ts"):
            continue
        dt = float(ev["ts"]) - now
        if -after_min * 60 <= dt <= before_min * 60:
            when = "in" if dt >= 0 else "vor"
            return f"News-Sperre: {ev.get('country')} {ev.get('title')} {when} {abs(dt) / 60:.0f} min (High Impact)"
    return None


class NewsWire:
    def __init__(self, event_store, config: Optional[Dict[str, Any]] = None,
                 client: Optional[httpx.AsyncClient] = None, now_fn=time.time):
        self.store, self.now = event_store, now_fn
        self.cfg = config if config is not None else load_config()
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=True,
                                                  headers={"User-Agent": USER_AGENT})
        default = float(self.cfg.get("default_min_interval_s", 60))
        self.sources: List[Dict[str, Any]] = [
            {"name": f.get("name") or f["url"], "url": f["url"], "every": max(10.0, float(f.get("min_interval_s", default))),
             "kind": "rss"} for f in self.cfg.get("feed") or [] if f.get("url")]
        cal = self.cfg.get("calendar") or {}
        if cal.get("url"):
            self.sources.append({"name": "calendar", "url": cal["url"], "kind": "calendar",
                                 "every": max(300.0, float(cal.get("min_interval_s", 1800)))})
        for s in self.sources:
            s.update(next_at=0.0, etag=None, modified=None, failures=0)
        self.keywords = {epic: [re.compile(k, re.IGNORECASE) for k in words]
                         for epic, words in (self.cfg.get("keywords") or {}).items()}
        self.seen: Dict[str, None] = {}
        try:   # after a restart, headlines already stored are not "new" again (no duplicate alerts)
            for e in event_store.get_events(types=["NEWS_ITEM"], last=MAX_SEEN // 2):
                p = json.loads(e.get("payload_json") or "{}")
                self.seen[self._key(p.get("source", ""), p.get("guid", ""))] = None
        except Exception as e:
            logger.warning(f"news history not loaded: {e}")
        self._calendar_digest: Optional[str] = None
        self._source_warm = False
        self.stats = {"items": 0, "alerts": 0, "errors": 0, "last_ok": None}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _key(source: str, guid: str) -> str:
        return hashlib.sha1(f"{source}|{guid}".encode()).hexdigest()

    def _fresh(self, published: Optional[str]) -> bool:
        """Only recent headlines alert agents. Without a date, only items after the source's first fetch count."""
        if published:
            try:
                return self.now() - datetime.fromisoformat(published).timestamp() <= FRESH_HOURS * 3600
            except ValueError:
                return False
        return self._source_warm

    def _epics(self, text: str) -> List[str]:
        return [e for e, rxs in self.keywords.items() if any(rx.search(text) for rx in rxs)]

    def _remember(self, key: str) -> bool:
        if key in self.seen:
            return False
        self.seen[key] = None
        if len(self.seen) > MAX_SEEN:
            for k in list(self.seen)[: MAX_SEEN // 4]:
                del self.seen[k]
        return True

    async def _fetch(self, src: Dict[str, Any]) -> Optional[str]:
        headers = {}
        if src["etag"]:
            headers["If-None-Match"] = src["etag"]
        if src["modified"]:
            headers["If-Modified-Since"] = src["modified"]
        async with self.client.stream("GET", src["url"], headers=headers) as r:
            body = bytearray()
            if r.status_code == 200:
                async for chunk in r.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise RuntimeError("response too large")
        now = self.now()
        if r.status_code == 304:
            src["failures"], src["next_at"] = 0, now + src["every"]
            return None
        if r.status_code == 429 or r.status_code >= 500:
            src["failures"] += 1
            retry = r.headers.get("Retry-After")
            wait = float(retry) if retry and retry.isdigit() else src["every"] * (2 ** min(src["failures"], 5))
            src["next_at"] = now + max(wait, src["every"])
            raise RuntimeError(f"HTTP {r.status_code}")
        r.raise_for_status()
        src["etag"], src["modified"] = r.headers.get("ETag"), r.headers.get("Last-Modified")
        src["failures"], src["next_at"] = 0, now + src["every"]
        return bytes(body).decode(r.encoding or "utf-8", errors="replace")

    # ------------------------------------------------------------------ handlers
    def _handle_rss(self, src: Dict[str, Any], text: str) -> int:
        new = 0
        for it in parse_feed(text):
            key = self._key(src["name"], it["guid"])
            if not self._remember(key):
                continue
            title, hits_t = screen(it["title"], limit=300)
            summary, hits_s = screen(it["summary"], limit=300)
            link, hits_l = screen(safe_link(it["link"]), limit=500)
            tainted = bool(hits_t or hits_s or hits_l)
            epics = [] if tainted else self._epics(f"{it['title']} {it['summary']}")
            fresh = self._fresh(it["published"])
            payload = {"source": src["name"], "guid": it["guid"], "title": title, "summary": summary,
                       "link": "" if hits_l else link, "published": it["published"], "epics": epics,
                       "tainted": tainted, "fresh": fresh}
            self.store.append("NEWS", "NEWS_ITEM", payload)
            new += 1
            if epics and fresh:
                self.store.append("NEWS", "NEWS_ALERT", payload)
                self.stats["alerts"] += 1
        self.stats["items"] += new
        return new

    def _handle_calendar(self, text: str) -> None:
        rows = json.loads(text)
        high = []
        for row in rows if isinstance(rows, list) else []:
            if str(row.get("impact", "")).lower() != "high":
                continue
            try:
                ts = datetime.fromisoformat(str(row.get("date"))).astimezone(timezone.utc).timestamp()
            except ValueError:
                continue
            title, hits = screen(str(row.get("title", "")), limit=120)
            country = str(row.get("country", "")).upper()
            if hits or not re.fullmatch(r"[A-Z]{2,4}", country):
                continue
            num = lambda v: v if re.fullmatch(r"[-+0-9.,%kKmMbBT<> ]{0,20}", v) else ""   # forecast/previous: figures only
            high.append({"title": title, "country": country, "ts": ts,
                         "time_utc": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="minutes"),
                         "forecast": num(str(row.get("forecast", ""))[:20]),
                         "previous": num(str(row.get("previous", ""))[:20])})
        high.sort(key=lambda e: e["ts"])
        digest = hashlib.sha1(json.dumps(high, sort_keys=True).encode()).hexdigest()
        if digest != self._calendar_digest:
            self._calendar_digest = digest
            cal = self.cfg.get("calendar") or {}
            self.store.append("NEWS", "ECON_CALENDAR", {
                "high_impact": high, "source": "ForexFactory (faireconomy.media)",
                "blackout_before_min": cal.get("blackout_before_min", 15),
                "blackout_after_min": cal.get("blackout_after_min", 15)})

    def _calendar_failed(self, reason: str) -> None:
        """Without a calendar the desk's news blackout is off: say so (once per 6 h), never silently."""
        now = self.now()
        if now - getattr(self, "_degraded_at", -1e12) >= 6 * 3600:
            self._degraded_at = now
            self.store.append("NEWS", "NEWS_DEGRADED", {"reason": f"calendar unavailable: {reason}"})

    # ------------------------------------------------------------------ loop
    async def tick(self) -> int:
        now, new = self.now(), 0
        for src in self.sources:
            if now < src["next_at"]:
                continue
            src["next_at"] = now + src["every"]           # also on failure: never hammer a source
            try:
                text = await self._fetch(src)
                if text is None:
                    continue
                if src["kind"] == "calendar":
                    self._handle_calendar(text)
                else:
                    self._source_warm = src.get("warm", False)
                    new += self._handle_rss(src, text)
                    src["warm"] = True
                self.stats["last_ok"] = now
            except Exception as e:
                self.stats["errors"] += 1
                logger.warning(f"news source {src['name']} failed: {type(e).__name__}: {str(e)[:120]}")
                if src["kind"] == "calendar":
                    self._calendar_failed(f"{type(e).__name__}: {str(e)[:120]}")
        return new

    async def run(self, is_running=lambda: True) -> None:
        poll = max(1.0, float(self.cfg.get("poll_seconds", 5)))
        logger.info(f"News wire started: {len(self.sources)} sources, check every {poll:g}s.")
        while is_running():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"news tick error: {e}")
            await asyncio.sleep(poll)

    async def close(self) -> None:
        await self.client.aclose()
