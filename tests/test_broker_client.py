import json

import httpx
import pytest

from src.firm.broker.client import CapitalComClient, DEMO_BASE_URL


def make_client(handler, **kwargs) -> CapitalComClient:
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport, timeout=5.0)
    return CapitalComClient("test_key", "test_id", "test_pass", client=async_client, **kwargs)


@pytest.mark.asyncio
async def test_broker_client_init():
    client = CapitalComClient("test_key", "test_id", "test_pass")
    assert client.api_key == "test_key"
    assert client.base_url == DEMO_BASE_URL
    await client.close()


@pytest.mark.asyncio
async def test_call_without_session_logs_in_first_and_surfaces_auth_errors():
    """No session -> the client logs in by itself (recovery after a failed start); a bad login raises."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(401, json={"errorCode": "error.invalid.api.key"})

    client = make_client(handler)
    with pytest.raises(ConnectionError, match="authentication failed"):
        await client.get_account_details()
    assert seen == ["/api/v1/session"]
    await client.close()


@pytest.mark.asyncio
async def test_get_positions_raises_instead_of_reporting_flat():
    """An API error must never look like an empty book (reconciler/kill switch would act on it)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session"):
            return httpx.Response(200, headers={"CST": "c", "X-SECURITY-TOKEN": "t"}, json={})
        return httpx.Response(500, json={"errorCode": "error.internal"})

    client = make_client(handler)
    await client.authenticate()
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_positions()
    await client.close()


@pytest.mark.asyncio
async def test_base_url_resolution_and_normalisation(monkeypatch):
    monkeypatch.delenv("CAPITAL_BASE_URL", raising=False)
    monkeypatch.setenv("CAPITAL_ENV", "demo")
    monkeypatch.setenv("CAPITAL_BASE_URL_DEMO", "https://example.test/api/v1")
    demo = CapitalComClient("k", "i", "p")
    assert demo.base_url == "https://example.test"
    await demo.close()

    monkeypatch.setenv("CAPITAL_ENV", "live")
    monkeypatch.setenv("CAPITAL_BASE_URL_LIVE", "https://live.test")
    live = CapitalComClient("k", "i", "p")
    assert live.base_url == "https://live.test"
    await live.close()


@pytest.mark.asyncio
async def test_authenticate_uses_v1_session_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("X-CAP-API-KEY")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"CST": "cst-token", "X-SECURITY-TOKEN": "security-token"},
            json={},
        )

    client = make_client(handler)
    assert await client.authenticate() is True
    assert seen["url"] == f"{DEMO_BASE_URL}/api/v1/session"
    assert seen["key"] == "test_key"
    assert seen["body"] == {"identifier": "test_id", "password": "test_pass"}
    assert client.session.cst == "cst-token"
    await client.close()


@pytest.mark.asyncio
async def test_account_details_are_normalised():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session"):
            return httpx.Response(200, headers={"CST": "c", "X-SECURITY-TOKEN": "s"}, json={})
        if request.url.path.endswith("/accounts"):
            return httpx.Response(200, json={"accounts": [{
                "currency": "CHF",
                "balance": {
                    "balance": 1000.0,
                    "equity": 1012.5,
                    "available": 900.0,
                    "profitLoss": 12.5,
                },
            }]})
        return httpx.Response(404)

    client = make_client(handler)
    await client.authenticate()
    details = await client.get_account_details()
    assert details == {
        "balance": 1000.0,
        "equity": 1012.5,
        "currency": "CHF",
        "account_id": None,
        "available": 900.0,
        "profit_loss": 12.5,
        "source": "broker",
    }
    await client.close()


@pytest.mark.asyncio
async def test_positions_are_flattened():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session"):
            return httpx.Response(200, headers={"CST": "c", "X-SECURITY-TOKEN": "s"}, json={})
        return httpx.Response(200, json={"positions": [{
            "position": {
                "dealId": "deal-1", "direction": "BUY", "size": 0.01, "level": 60000.0,
                "bid": 60100.0, "offer": 60110.0, "upl": 1.0,
                "stopLevel": 59000.0, "profitLevel": 62000.0, "currency": "USD",
            },
            "market": {"epic": "BTCUSD", "currency": "USD"},
        }]})

    client = make_client(handler)
    await client.authenticate()
    positions = await client.get_positions()
    assert positions == [{
        "dealId": "deal-1",
        "epic": "BTCUSD",
        "direction": "BUY",
        "volume": 0.01,
        "level": 60000.0,
        "bid": 60100.0,
        "offer": 60110.0,
        "upl": 1.0,
        "stop_level": 59000.0,
        "profit_level": 62000.0,
        "currency": "USD",
    }]
    await client.close()


@pytest.mark.asyncio
async def test_place_order_confirms_the_deal():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/session"):
            return httpx.Response(200, headers={"CST": "c", "X-SECURITY-TOKEN": "s"}, json={})
        if request.method == "POST" and request.url.path.endswith("/positions"):
            body = json.loads(request.content)
            assert body["direction"] == "BUY"
            assert body["size"] == 0.01
            assert body["stopLevel"] == 59000.0
            return httpx.Response(200, json={"dealReference": "ref-123"})
        if request.url.path.endswith("/confirms/ref-123"):
            return httpx.Response(200, json={
                "dealStatus": "ACCEPTED", "dealId": "deal-9", "level": 60010.5, "size": 0.01,
            })
        return httpx.Response(404)

    client = make_client(handler)
    await client.authenticate()
    result = await client.place_order("BTCUSD", "BUY", 0.01, stop_loss=59000.0)
    assert result["dealId"] == "deal-9"
    assert result["price"] == 60010.5
    assert result["status"] == "ACCEPTED"
    assert ("POST", "/api/v1/positions") in calls
    assert ("GET", "/api/v1/confirms/ref-123") in calls
    await client.close()


@pytest.mark.asyncio
async def test_place_order_rejects_unconfirmed_deal():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session"):
            return httpx.Response(200, headers={"CST": "c", "X-SECURITY-TOKEN": "s"}, json={})
        if request.method == "POST" and request.url.path.endswith("/positions"):
            return httpx.Response(200, json={"dealReference": "ref-bad"})
        return httpx.Response(200, json={"dealStatus": "REJECTED", "reason": "MARKET_CLOSED"})

    client = make_client(handler)
    await client.authenticate()
    with pytest.raises(ConnectionError, match="MARKET_CLOSED"):
        await client.place_order("BTCUSD", "BUY", 0.01, stop_loss=59000.0)
    await client.close()


@pytest.mark.asyncio
async def test_flatten_all_uses_delete():
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session"):
            return httpx.Response(200, headers={"CST": "c", "X-SECURITY-TOKEN": "s"}, json={})
        if request.method == "GET" and request.url.path.endswith("/positions"):
            return httpx.Response(200, json={"positions": [{
                "position": {"dealId": "deal-1", "direction": "BUY", "size": 0.01},
                "market": {"epic": "BTCUSD"},
            }]})
        if request.method == "DELETE":
            methods.append(request.method)
            return httpx.Response(200, json={})
        return httpx.Response(404)

    client = make_client(handler)
    await client.authenticate()
    await client.flatten_all()
    assert methods == ["DELETE"]
    await client.close()


@pytest.mark.asyncio
async def test_account_details_use_the_preferred_account():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session"):
            return httpx.Response(200, headers={"CST": "c", "X-SECURITY-TOKEN": "s"}, json={})
        return httpx.Response(200, json={"accounts": [
            {"accountId": "SB1", "currency": "GBP", "balance": {"balance": 50.0}},
            {"accountId": "CFD1", "preferred": True, "currency": "CHF", "balance": {"balance": 1000.0}}]})

    client = make_client(handler)
    await client.authenticate()
    details = await client.get_account_details()
    assert details["account_id"] == "CFD1" and details["balance"] == 1000.0
    await client.close()
