"""Historische Kerzen laden (seitenweise), mit Spread, als CSV cachen. Ohne Daten kein Backtest."""
from __future__ import annotations

import csv
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import lab

FIELDS = ["ts", "o", "h", "l", "c", "spread", "v"]


def parse_prices(prices: list) -> list:
    """Capital.com-Preisobjekte -> Kerzen mit Mid-OHLC und Spread (Ask-Bid am Schluss)."""
    out = []
    for p in prices:
        def mid(k):
            d = p.get(k) or {}
            b, a = d.get("bid"), d.get("ask")
            return (float(b) + float(a)) / 2 if b is not None and a is not None else None
        ts = lab.parse_ts(p.get("snapshotTimeUTC") or p.get("snapshotTime"))
        o, h, l, c = (mid(k) for k in ("openPrice", "highPrice", "lowPrice", "closePrice"))
        if None in (ts, o, h, l, c):
            continue
        cp = p.get("closePrice") or {}
        sp = (float(cp["ask"]) - float(cp["bid"])) if cp.get("ask") is not None and cp.get("bid") is not None else 0.0
        out.append({"ts": ts, "o": o, "h": h, "l": l, "c": c, "spread": max(sp, 0.0), "v": p.get("lastTradedVolume") or 0})
    return out


def fetch_range(fetch_page, epic, resolution, days, page=1000, sleep=0.3):
    """fetch_page(epic, resolution, date_from_iso, date_to_iso) -> Liste Preisobjekte (max `page`). Dedupliziert + sortiert."""
    step = lab.RES_SECONDS[resolution]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    cur, seen, candles = start, set(), []
    while cur < end:
        nxt = min(cur + timedelta(seconds=step * (page - 1)), end)
        rows = parse_prices(fetch_page(epic, resolution, cur.strftime("%Y-%m-%dT%H:%M:%S"), nxt.strftime("%Y-%m-%dT%H:%M:%S")))
        for r in rows:
            if r["ts"] not in seen:
                seen.add(r["ts"]); candles.append(r)
        cur = nxt + timedelta(seconds=step)
        time.sleep(sleep)
    candles.sort(key=lambda x: x["ts"])
    return candles


def quality_report(candles, resolution):
    """Luecken, Duplikate, Nullbereiche: schlechte Daten erzeugen scheinbare Edges."""
    step = lab.RES_SECONDS[resolution]
    gaps = [(candles[i - 1]["ts"], candles[i]["ts"]) for i in range(1, len(candles)) if candles[i]["ts"] - candles[i - 1]["ts"] > 3 * step]
    flat = sum(1 for c in candles if c["h"] == c["l"])
    return {"bars": len(candles), "big_gaps": len(gaps), "flat_bars": flat,
            "span_days": (candles[-1]["ts"] - candles[0]["ts"]) / 86400 if candles else 0,
            "avg_spread_pct": (sum(c["spread"] / c["c"] for c in candles) / len(candles) * 100) if candles else None}


def save_csv(path, candles):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS); w.writeheader()
        for c in candles:
            w.writerow({k: c.get(k, 0) for k in FIELDS})


def load_csv(path):
    with open(path, newline="") as f:
        return [{k: float(v) for k, v in r.items()} for r in csv.DictReader(f)]


async def afetch_range(fetch_page, epic, resolution, days, page=1000, sleep=0.3):
    """Wie fetch_range, aber mit async fetch_page (fuer den httpx-Client des Projekts)."""
    import asyncio
    step = lab.RES_SECONDS[resolution]
    end = datetime.now(timezone.utc)
    cur, seen, candles = end - timedelta(days=days), set(), []
    while cur < end:
        nxt = min(cur + timedelta(seconds=step * (page - 1)), end)
        rows = parse_prices(await fetch_page(epic, resolution, cur.strftime("%Y-%m-%dT%H:%M:%S"), nxt.strftime("%Y-%m-%dT%H:%M:%S")))
        for r in rows:
            if r["ts"] not in seen:
                seen.add(r["ts"]); candles.append(r)
        cur = nxt + timedelta(seconds=step)
        await asyncio.sleep(sleep)
    candles.sort(key=lambda x: x["ts"])
    return candles
