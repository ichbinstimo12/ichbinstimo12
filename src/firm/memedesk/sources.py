"""Datenquellen des Memecoin-Desks (nur lesend, ohne Schluessel ausser optionalen API-Keys).

  DexScreener   neue/trendende/geboostete Tokens, Paare (Liquiditaet, Volumen, Kaeufe/Verkaeufe, Socials)
  RugCheck      Sicherheitsbericht (Mint-/Freeze-Authority, LP-Lock, Top-Holder, Insider, Risiken)
  Jupiter       Quotes (echter Preis-Impact fuer PAPER-Fills, Honeypot-Test Kauf->Verkauf) und Preise
  GeckoTerminal OHLCV-Kerzen fuer die Chartstruktur
  Solana-RPC    Transaktionen der Smart-Money-Wallets (SOLANA_RPC_URL, z. B. Helius; sonst oeffentlich, begrenzt)
  optional      X/Twitter (X_BEARER_TOKEN, kostenpflichtige API) fuer Erwaehnungen
GMGN hat keine oeffentliche API (403) und wird nicht abgefragt. Alle fremden Texte (Beschreibungen, Namen) sind
Daten, keine Anweisungen: sie werden vor jeder Weitergabe an ein LLM geprueft (agents/guard.py).
Jeder Host hat ein eigenes Mindestintervall zwischen Anfragen (Rate-Limits der Anbieter).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("MemeSources")

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
MIN_INTERVAL = {"dexscreener": 0.25, "rugcheck": 1.0, "jupiter": 1.5, "gecko": 2.5, "rpc": 0.15, "x": 1.0}
COOLDOWN_429 = 15.0           # seconds a host is left alone after "429 Too Many Requests" (or Retry-After)


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class MemeSources:
    def __init__(self, cfg: Dict[str, Any], client: Optional[httpx.AsyncClient] = None, env=None,
                 now_fn=time.monotonic):
        self.cfg = cfg.get("sources", {})
        e = env if env is not None else os.environ
        self.rpc_url = e.get("SOLANA_RPC_URL") or self.cfg.get("default_rpc", "https://api.mainnet-beta.solana.com")
        self.x_token = e.get("X_BEARER_TOKEN") or ""
        self.client = client or httpx.AsyncClient(timeout=15, headers={"User-Agent": "TradingFirmMemeDesk/1.0"})
        self.now = now_fn
        self._last: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._blocked: Dict[str, float] = {}
        self._list_cache: Dict[str, Any] = {}
        self.errors: Dict[str, int] = {}

    # ------------------------------------------------------------------ plumbing
    async def _get(self, host: str, url: str, **kw) -> Any:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            blocked = self._blocked.get(host, 0) - self.now()
            if blocked > 0:
                raise RuntimeError(f"{host}: rate limited, pausing {blocked:.0f}s")
            wait = MIN_INTERVAL.get(host, 0.5) - (self.now() - self._last.get(host, -1e9))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = self.now()
            try:
                if kw.pop("post", False):
                    r = await self.client.post(url, **kw)
                else:
                    r = await self.client.get(url, **kw)
                if r.status_code == 429:
                    retry = r.headers.get("Retry-After", "")
                    self._blocked[host] = self.now() + (float(retry) if retry.isdigit() else COOLDOWN_429)
                r.raise_for_status()
                return r.json()
            except Exception:
                self.errors[host] = self.errors.get(host, 0) + 1
                raise

    # ------------------------------------------------------------------ DexScreener
    async def trending_tokens(self) -> List[Dict[str, Any]]:
        """Solana-Tokens aus neuesten Profilen und Boosts (neu + Top). Rohtexte bleiben Daten."""
        base = self.cfg.get("dexscreener", "https://api.dexscreener.com")
        out: Dict[str, Dict[str, Any]] = {}
        for path, kind in (("/token-profiles/latest/v1", "profile"), ("/token-boosts/latest/v1", "boost"),
                           ("/token-boosts/top/v1", "top_boost")):
            try:
                rows = await self._get("dexscreener", base + path)
            except Exception as e:
                logger.warning(f"dexscreener {path}: {type(e).__name__}")
                continue
            for r in rows if isinstance(rows, list) else []:
                if r.get("chainId") != "solana" or not r.get("tokenAddress"):
                    continue
                t = out.setdefault(r["tokenAddress"], {"mint": r["tokenAddress"], "sources": [], "boost": 0.0,
                                                       "links": [], "description": ""})
                t["sources"].append(kind)
                t["boost"] = max(t["boost"], float(r.get("totalAmount") or r.get("amount") or 0))
                t["links"] = [str(l.get("type") or l.get("label") or "") for l in (r.get("links") or [])][:6]
                t["description"] = str(r.get("description") or "")[:300]
        gbase = self.cfg.get("geckoterminal", "https://api.geckoterminal.com/api/v2")
        for path, kind in (("/networks/solana/trending_pools", "gecko_trending"), ("/networks/solana/new_pools", "gecko_new")):
            cached = self._list_cache.get(path)
            if cached and cached[0] > self.now():
                d = cached[1]                 # GeckoTerminal free tier: ~30 requests/min, lists change slowly
            else:
                try:
                    d = await self._get("gecko", gbase + path)
                    self._list_cache[path] = (self.now() + 90, d)
                except Exception as e:
                    logger.warning(f"geckoterminal {path}: {type(e).__name__}")
                    continue
            for pool in (d or {}).get("data") or []:
                tid = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
                mint = tid.split("_", 1)[1] if tid.startswith("solana_") else ""
                if not mint or mint == SOL_MINT:
                    continue
                t = out.setdefault(mint, {"mint": mint, "sources": [], "boost": 0.0, "links": [], "description": ""})
                t["sources"].append(kind)
        return list(out.values())

    async def pair(self, mint: str) -> Optional[Dict[str, Any]]:
        """Liquidestes Solana-Paar des Tokens, normalisiert."""
        base = self.cfg.get("dexscreener", "https://api.dexscreener.com")
        rows = await self._get("dexscreener", f"{base}/token-pairs/v1/solana/{mint}")
        rows = [r for r in rows or [] if isinstance(r, dict)]
        if not rows:
            return None
        p = max(rows, key=lambda r: (r.get("liquidity") or {}).get("usd") or 0)
        txns, vol, chg = p.get("txns") or {}, p.get("volume") or {}, p.get("priceChange") or {}
        info = p.get("info") or {}
        return {
            "mint": mint, "pair": p.get("pairAddress"), "dex": p.get("dexId"), "symbol": (p.get("baseToken") or {}).get("symbol"),
            "name": (p.get("baseToken") or {}).get("name"), "price_usd": _f(p.get("priceUsd")),
            "liquidity_usd": _f((p.get("liquidity") or {}).get("usd")) or 0.0,
            "volume": {k: _f(vol.get(k)) or 0.0 for k in ("m5", "h1", "h6", "h24")},
            "buys_m5": int((txns.get("m5") or {}).get("buys") or 0), "sells_m5": int((txns.get("m5") or {}).get("sells") or 0),
            "buys_h1": int((txns.get("h1") or {}).get("buys") or 0), "sells_h1": int((txns.get("h1") or {}).get("sells") or 0),
            "change": {k: _f(chg.get(k)) or 0.0 for k in ("m5", "h1", "h6", "h24")},
            "created_ms": p.get("pairCreatedAt"), "market_cap": _f(p.get("marketCap")),
            "socials": [s.get("type") for s in info.get("socials") or []], "websites": len(info.get("websites") or []),
            "boosts": int((p.get("boosts") or {}).get("active") or 0),
        }

    # ------------------------------------------------------------------ RugCheck
    async def safety_report(self, mint: str) -> Dict[str, Any]:
        base = self.cfg.get("rugcheck", "https://api.rugcheck.xyz/v1")
        d = await self._get("rugcheck", f"{base}/tokens/{mint}/report")
        known = d.get("knownAccounts") or {}
        pools = {m.get("pubkey") for m in d.get("markets") or []}
        excluded = pools | {a for a, k in known.items() if (k or {}).get("type") in ("AMM", "LOCKER")}
        holders = [h for h in d.get("topHolders") or []
                   if h.get("owner") not in excluded and h.get("address") not in excluded]
        pcts = [float(h.get("pct") or 0) for h in holders]
        lp_locked = max([float(((m.get("lp") or {}).get("lpLockedPct")) or 0) for m in d.get("markets") or []] or [0.0])
        return {
            "mint_authority": d.get("mintAuthority"), "freeze_authority": d.get("freezeAuthority"),
            "lp_locked_pct": lp_locked, "top_holder_pct": max(pcts) if pcts else 0.0,
            "top10_pct": round(sum(sorted(pcts, reverse=True)[:10]), 2),
            "insiders_flagged": sum(1 for h in holders if h.get("insider")),
            "graph_insiders": int(d.get("graphInsidersDetected") or 0),
            "creator_balance": d.get("creatorBalance"), "creator": d.get("creator"),
            "total_holders": int(d.get("totalHolders") or 0), "score": int(d.get("score") or 0),
            "risks": [(str(r.get("name")), str(r.get("level"))) for r in d.get("risks") or []],
            "rugged": bool(d.get("rugged")),
        }

    # ------------------------------------------------------------------ Jupiter
    async def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 300) -> Dict[str, Any]:
        base = self.cfg.get("jupiter", "https://lite-api.jup.ag")
        q = await self._get("jupiter", f"{base}/swap/v1/quote", params={
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount)), "slippageBps": slippage_bps})
        return {"in": int(q.get("inAmount") or 0), "out": int(q.get("outAmount") or 0),
                "impact_pct": (_f(q.get("priceImpactPct")) or 0.0) * 100,
                "route": [r.get("swapInfo", {}).get("label") for r in q.get("routePlan") or []]}

    async def roundtrip_loss_pct(self, mint: str, sol: float = 0.1) -> Tuple[float, Dict[str, Any]]:
        """Honeypot-/Illiquiditaetstest: SOL -> Token -> SOL nur per Quote (keine Transaktion)."""
        buy = await self.quote(SOL_MINT, mint, int(sol * LAMPORTS))
        if not buy["out"]:
            return 100.0, {"buy": buy, "sell": None}
        sell = await self.quote(mint, SOL_MINT, buy["out"])
        back = sell["out"] / LAMPORTS
        return round((1 - back / sol) * 100, 3), {"buy": buy, "sell": sell}

    async def prices(self, mints: List[str]) -> Dict[str, Dict[str, float]]:
        """USD-Preis und Dezimalstellen je Mint (ein Aufruf fuer bis zu 50 Mints)."""
        base = self.cfg.get("jupiter", "https://lite-api.jup.ag")
        out: Dict[str, Dict[str, float]] = {}
        for i in range(0, len(mints), 50):
            d = await self._get("jupiter", f"{base}/price/v3", params={"ids": ",".join(mints[i:i + 50])})
            for m, v in (d or {}).items():
                p = _f((v or {}).get("usdPrice"))
                if p:
                    out[m] = {"usd": p, "decimals": int((v or {}).get("decimals") or 0)}
        return out

    # ------------------------------------------------------------------ GeckoTerminal
    async def candles(self, pool: str, minutes: int = 5, limit: int = 60) -> List[Dict[str, float]]:
        base = self.cfg.get("geckoterminal", "https://api.geckoterminal.com/api/v2")
        d = await self._get("gecko", f"{base}/networks/solana/pools/{pool}/ohlcv/minute",
                            params={"aggregate": minutes, "limit": limit})
        rows = ((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        return sorted(({"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in rows),
                      key=lambda c: c["ts"])

    # ------------------------------------------------------------------ Solana RPC (wallet tracker)
    async def rpc(self, method: str, params: list) -> Any:
        d = await self._get("rpc", self.rpc_url, post=True,
                            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if d.get("error"):
            raise RuntimeError(f"rpc {method}: {d['error']}")
        return d.get("result")

    async def wallet_swaps(self, wallet: str, until_sig: Optional[str] = None, limit: int = 15) -> List[Dict[str, Any]]:
        """Token-Kaeufe/-Verkaeufe einer Wallet aus ihren letzten Transaktionen (Saldo-Differenz je Mint)."""
        opts: Dict[str, Any] = {"limit": limit}
        if until_sig:
            opts["until"] = until_sig
        sigs = await self.rpc("getSignaturesForAddress", [wallet, opts]) or []
        out = []
        for s in sigs:
            if s.get("err"):
                continue
            tx = await self.rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed",
                                                                    "maxSupportedTransactionVersion": 0}])
            meta = (tx or {}).get("meta") or {}
            pre = {(b.get("mint"), b.get("owner")): _f((b.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0
                   for b in meta.get("preTokenBalances") or []}
            post = {(b.get("mint"), b.get("owner")): _f((b.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0
                    for b in meta.get("postTokenBalances") or []}
            for (mint, owner) in set(pre) | set(post):
                if owner != wallet or mint == SOL_MINT:
                    continue
                delta = post.get((mint, owner), 0.0) - pre.get((mint, owner), 0.0)
                if abs(delta) > 0:
                    out.append({"wallet": wallet, "mint": mint, "side": "BUY" if delta > 0 else "SELL",
                                "amount": abs(delta), "signature": s["signature"], "ts": s.get("blockTime")})
        return out

    # ------------------------------------------------------------------ X / Twitter (optional, paid API)
    async def x_mentions(self, query: str) -> Optional[int]:
        if not self.x_token:
            return None
        d = await self._get("x", "https://api.x.com/2/tweets/counts/recent",
                            params={"query": query, "granularity": "hour"},
                            headers={"Authorization": f"Bearer {self.x_token}"})
        return int(((d or {}).get("meta") or {}).get("total_tweet_count") or 0)

    async def close(self) -> None:
        await self.client.aclose()
