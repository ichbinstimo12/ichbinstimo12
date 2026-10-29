"""Memecoin-Desk: Datenquellen gegen echte (gespeicherte) Antworten von DexScreener, RugCheck, Jupiter, GeckoTerminal."""
import json
from pathlib import Path

import httpx
import pytest

from src.firm.memedesk.sources import MemeSources

FIX = Path(__file__).parent / "fixtures" / "memedesk"
MINT = "FEXGegqeVm5vZurqyC7qsmkLqWspVV1ACb9bbLvcpump"


def load(name):
    return json.loads((FIX / name).read_text())


def sources(handler):
    return MemeSources({"sources": {}}, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), env={},
                       now_fn=lambda: 1e9)


@pytest.mark.asyncio
async def test_pair_is_normalised():
    s = sources(lambda r: httpx.Response(200, json=load("pairs.json")))
    p = await s.pair(MINT)
    assert p["dex"] == "pumpswap" and p["liquidity_usd"] > 90000 and p["volume"]["m5"] > 0
    assert p["buys_m5"] == 416 and p["sells_m5"] == 213 and "twitter" in p["socials"]
    await s.close()


@pytest.mark.asyncio
async def test_safety_report_excludes_pool_accounts_from_holders():
    s = sources(lambda r: httpx.Response(200, json=load("rugcheck.json")))
    r = await s.safety_report(MINT)
    assert r["mint_authority"] is None and r["freeze_authority"] is None
    assert r["lp_locked_pct"] == 100 and r["total_holders"] > 1000
    assert r["top_holder_pct"] < 1.0 and r["decimals"] == 6   # the 5 % holder is the AMM pool, not a whale
    await s.close()


@pytest.mark.asyncio
async def test_roundtrip_quote_and_candles():
    q = load("jupiter_quote.json")

    def handler(req):
        if "quote" in req.url.path:
            if req.url.params["inputMint"].startswith("So111"):
                return httpx.Response(200, json=q)
            return httpx.Response(200, json=dict(q, inAmount=q["outAmount"], outAmount="95000000"))
        return httpx.Response(200, json=load("gecko_ohlcv.json"))
    s = sources(handler)
    loss, detail = await s.roundtrip_loss_pct(MINT, 0.1)
    assert loss == pytest.approx(5.0) and detail["buy"]["route"] == ["Pump.fun Amm"]
    cs = await s.candles("pool")
    assert cs[0]["ts"] < cs[-1]["ts"] and set(cs[0]) == {"ts", "o", "h", "l", "c", "v"}
    await s.close()


@pytest.mark.asyncio
async def test_wallet_swaps_from_token_balance_changes():
    W, M = "Wallet111", "MintAAA"

    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "getSignaturesForAddress":
            return httpx.Response(200, json={"result": [{"signature": "sig1", "blockTime": 1700, "err": None}]})
        return httpx.Response(200, json={"result": {"meta": {
            "preTokenBalances": [], "postTokenBalances": [
                {"mint": M, "owner": W, "uiTokenAmount": {"uiAmount": 1500.0}}]}}})
    s = sources(handler)
    swaps = await s.wallet_swaps(W)
    assert swaps == [{"wallet": W, "mint": M, "side": "BUY", "amount": 1500.0, "signature": "sig1", "ts": 1700}]
    await s.close()
