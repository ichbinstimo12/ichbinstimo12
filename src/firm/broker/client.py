import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BrokerClient")

DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"
LIVE_BASE_URL = "https://api-capital.backend-capital.com"
API_PREFIX = "/api/v1"

# Capital.com sessions expire after 10 minutes of inactivity. Refresh earlier.
SESSION_TTL_SECONDS = 540


@dataclass
class BrokerSession:
    cst: str
    x_security_token: str
    expires_at: float


class CapitalComClient:
    """
    Wrapper for the Capital.com REST API (v1).

    Endpoints follow https://open-api.capital.com:
        POST   /api/v1/session              -> CST + X-SECURITY-TOKEN headers
        GET    /api/v1/accounts             -> account balance/equity
        GET    /api/v1/markets              -> tradable markets
        GET    /api/v1/prices/{epic}        -> historical prices
        GET    /api/v1/positions            -> open positions
        POST   /api/v1/positions            -> place order (dealReference)
        GET    /api/v1/confirms/{ref}       -> real deal status + fill level
        DELETE /api/v1/positions/{dealId}   -> close position

    All responses are normalised to the shapes the firm expects, so the rest of
    the system never depends on Capital.com's nesting.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        identifier: Optional[str] = None,
        password: Optional[str] = None,
        base_url: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.api_key = api_key or os.getenv("CAPITAL_API_KEY")
        self.identifier = identifier or os.getenv("CAPITAL_IDENTIFIER")
        self.password = password or os.getenv("CAPITAL_PASSWORD")

        if base_url:
            self.base_url = self._normalize_base_url(base_url)
        else:
            self.base_url = self._resolve_base_url()

        if not all([self.api_key, self.identifier, self.password]):
            raise EnvironmentError("Missing required Capital.com credentials in .env or constructor")

        self.session: Optional[BrokerSession] = None
        self._client = client or httpx.AsyncClient(timeout=20.0)

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        """Accept both 'https://host' and 'https://host/api/v1' forms."""
        url = url.strip().rstrip("/")
        if url.endswith(API_PREFIX):
            url = url[: -len(API_PREFIX)]
        return url

    @classmethod
    def _resolve_base_url(cls) -> str:
        explicit = os.getenv("CAPITAL_BASE_URL")
        if explicit:
            return cls._normalize_base_url(explicit)

        env_name = (os.getenv("CAPITAL_ENV") or "").strip().lower()
        is_demo = os.getenv("IS_DEMO", "True").strip().lower() in ("1", "true", "yes", "on")
        if env_name:
            is_demo = env_name != "live"

        if is_demo:
            return cls._normalize_base_url(os.getenv("CAPITAL_BASE_URL_DEMO") or DEMO_BASE_URL)
        return cls._normalize_base_url(os.getenv("CAPITAL_BASE_URL_LIVE") or LIVE_BASE_URL)

    # ------------------------------------------------------------- session
    @property
    def is_demo(self) -> bool:
        """True when this client talks to the Capital.com demo environment."""
        return "demo" in self.base_url.lower()

    async def authenticate(self) -> bool:
        """Create a session and store the CST / X-SECURITY-TOKEN pair."""
        url = f"{self.base_url}{API_PREFIX}/session"
        headers = {"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"}
        payload = {"identifier": self.identifier, "password": self.password}

        try:
            response = await self._client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:300]
            logger.error(f"Authentication failed: HTTP {e.response.status_code} - {detail}")
            raise ConnectionError(f"Broker authentication failed: {e.response.status_code} {detail}") from e
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            raise ConnectionError(f"Unexpected error during broker authentication: {e}") from e

        cst = response.headers.get("CST")
        token = response.headers.get("X-SECURITY-TOKEN")
        if not cst or not token:
            raise ConnectionError("Authentication succeeded but CST / X-SECURITY-TOKEN headers are missing")

        self.session = BrokerSession(
            cst=cst,
            x_security_token=token,
            expires_at=time.time() + SESSION_TTL_SECONDS,
        )
        logger.info(f"Authenticated with Capital.com ({self.base_url})")
        return True

    async def _ensure_session(self) -> None:
        if self.session is None:
            raise ConnectionError("No active session. Call authenticate() first.")
        if time.time() >= self.session.expires_at:
            await self.authenticate()

    def _get_headers(self) -> Dict[str, str]:
        if not self.session:
            raise ConnectionError("No active session. Call authenticate() first.")
        return {
            "X-CAP-API-KEY": self.api_key,
            "CST": self.session.cst,
            "X-SECURITY-TOKEN": self.session.x_security_token,
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Authenticated request with one transparent re-auth on 401."""
        await self._ensure_session()
        url = f"{self.base_url}{API_PREFIX}{path}"
        response = await self._client.request(method, url, headers=self._get_headers(), **kwargs)
        if response.status_code == 401:
            logger.warning("Session expired (401). Re-authenticating once...")
            await self.authenticate()
            response = await self._client.request(method, url, headers=self._get_headers(), **kwargs)
        response.raise_for_status()
        return response

    # --------------------------------------------------------------- read
    async def get_account_details(self) -> Dict[str, Any]:
        """Normalised account snapshot: balance, equity, currency (no invented values)."""
        data = (await self._request("GET", "/accounts")).json()
        accounts = data.get("accounts") or []
        if not accounts:
            raise ConnectionError("Capital.com returned no accounts for this session")

        account = accounts[0]
        balance_obj = account.get("balance") or {}
        balance = balance_obj.get("balance")
        equity = balance_obj.get("equity", balance)
        return {
            "balance": balance,
            "equity": equity,
            "currency": account.get("currency"),
            "available": balance_obj.get("available"),
            "profit_loss": balance_obj.get("profitLoss"),
            "source": "broker",
        }

    async def get_market_list(self) -> List[Dict[str, Any]]:
        try:
            data = (await self._request("GET", "/markets")).json()
            return data.get("markets") or []
        except Exception as e:
            logger.error(f"Failed to get market list: {e}")
            return []

    async def get_historical_prices(
        self, epic: str, timeframe: str = "MINUTE", limit: int = 100
    ) -> List[Dict[str, Any]]:
        params = {"resolution": timeframe, "max": max(1, min(int(limit), 1000))}
        try:
            data = (await self._request("GET", f"/prices/{epic}", params=params)).json()
            return data.get("prices") or []
        except Exception as e:
            logger.error(f"Failed to get prices for {epic}: {e}")
            return []

    async def get_market_details(self, epic: str) -> Dict[str, Any]:
        """
        Normalised market snapshot + broker dealing rules for one instrument.

        The rules matter for correctness: min/max deal size and the size
        increment are enforced by the broker, and an order that violates them is
        rejected. We size positions against these real limits, never guesses.
        """
        data = (await self._request("GET", f"/markets/{epic}")).json()
        snapshot = data.get("snapshot") or {}
        rules = data.get("dealingRules") or {}

        def rule(name: str, field: str = "value"):
            return (rules.get(name) or {}).get(field)

        return {
            "epic": epic,
            "currency": (data.get("instrument") or {}).get("currency"),
            "status": snapshot.get("marketStatus"),
            "bid": snapshot.get("bid"),
            "ask": snapshot.get("offer"),
            "update_time": snapshot.get("updateTime"),
            "min_size": rule("minDealSize"),
            "max_size": rule("maxDealSize"),
            "size_step": rule("minSizeIncrement"),
            "min_stop_distance": rule("minStopOrProfitDistance"),
            "min_stop_unit": rule("minStopOrProfitDistance", "unit"),
        }

    async def get_candles(
        self, epic: str, resolution: str = "MINUTE_15", limit: int = 200
    ) -> List[Dict[str, Any]]:
        """OHLC candles with the mid price, ready for indicator maths."""
        prices = await self.get_historical_prices(epic, timeframe=resolution, limit=limit)

        def mid(book: Any) -> Optional[float]:
            if not isinstance(book, dict):
                return None
            bid, ask = book.get("bid"), book.get("ask")
            if bid is None or ask is None:
                return None
            return (float(bid) + float(ask)) / 2.0

        candles: List[Dict[str, Any]] = []
        for item in prices:
            o, h, l, c = (
                mid(item.get("openPrice")),
                mid(item.get("highPrice")),
                mid(item.get("lowPrice")),
                mid(item.get("closePrice")),
            )
            if None in (o, h, l, c):
                continue
            candles.append({
                "t": item.get("snapshotTimeUTC") or item.get("snapshotTime"),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": item.get("lastTradedVolume") or 0,
            })
        return candles

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Flat list: dealId, epic, direction, volume, level, upl (live P/L)."""
        try:
            data = (await self._request("GET", "/positions")).json()
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

        positions: List[Dict[str, Any]] = []
        for item in data.get("positions") or []:
            pos = item.get("position") or {}
            market = item.get("market") or {}
            positions.append({
                "dealId": pos.get("dealId"),
                "epic": market.get("epic"),
                "direction": pos.get("direction"),
                "volume": pos.get("size", pos.get("dealSize", 0.0)),
                "level": pos.get("level"),
                "bid": pos.get("bid"),
                "offer": pos.get("offer"),
                "upl": pos.get("upl"),
                "stop_level": pos.get("stopLevel"),
                "profit_level": pos.get("profitLevel"),
                "currency": market.get("currency") or pos.get("currency"),
            })
        return positions

    # -------------------------------------------------------------- write
    async def place_order(
        self,
        epic: str,
        side: str,
        size: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Place an order and wait for the real confirmation.

        Capital.com answers POST /positions with a dealReference; the actual
        status and fill level come from GET /confirms/{dealReference}. We never
        report a fill we did not confirm.
        """
        payload: Dict[str, Any] = {
            "epic": epic,
            "direction": "BUY" if str(side).upper() == "BUY" else "SELL",
            "size": size,
        }
        if stop_loss is not None:
            payload["stopLevel"] = stop_loss
        if take_profit is not None:
            payload["profitLevel"] = take_profit

        try:
            response = await self._request("POST", "/positions", json=payload)
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:300]
            logger.error(f"Order rejected: HTTP {e.response.status_code} - {detail}")
            raise ConnectionError(f"Broker order rejected: {e.response.status_code} {detail}") from e

        deal_reference = (response.json() or {}).get("dealReference")
        if not deal_reference:
            raise ConnectionError("Broker accepted the request but returned no dealReference")

        confirm = (await self._request("GET", f"/confirms/{deal_reference}")).json()
        status = str(confirm.get("dealStatus") or "").upper()
        if status != "ACCEPTED":
            reason = confirm.get("reason") or confirm.get("dealStatus") or "unknown"
            logger.error(f"Deal {deal_reference} not accepted: {reason}")
            raise ConnectionError(f"Broker rejected deal: {reason}")

        return {
            "dealId": confirm.get("dealId"),
            "dealReference": deal_reference,
            "price": confirm.get("level"),
            "status": status,
            "size": confirm.get("size"),
        }

    async def close_position(self, deal_id: str) -> Dict[str, Any]:
        """
        Close one open position by deal id (used for signal-based exits).

        Returns the broker's own confirmation, including the realised profit in
        account currency. If the confirmation is unavailable we still report the
        close, but without a made-up number.
        """
        response = await self._request("DELETE", f"/positions/{deal_id}")
        try:
            body = response.json() or {}
        except Exception:
            body = {}

        reference = body.get("dealReference")
        if not reference:
            return body

        try:
            confirm = (await self._request("GET", f"/confirms/{reference}")).json() or {}
        except Exception as e:
            logger.warning(f"Close {deal_id} accepted but confirmation failed: {e}")
            return body

        status = str(confirm.get("dealStatus") or "").upper()
        if status and status != "ACCEPTED":
            reason = confirm.get("reason") or status
            raise ConnectionError(f"Broker rejected close of {deal_id}: {reason}")

        return {
            "dealId": confirm.get("dealId") or deal_id,
            "dealReference": reference,
            "status": status,
            "level": confirm.get("level"),
            "size": confirm.get("size"),
            "profit": confirm.get("profit"),
            "profit_currency": confirm.get("profitCurrency"),
        }

    async def flatten_all(self) -> None:
        """Close every open position (used by the kill switch)."""
        positions = await self.get_positions()
        if not positions:
            logger.info("No positions to flatten.")
            return

        logger.info(f"Flattening {len(positions)} open position(s)...")
        for pos in positions:
            deal_id = pos.get("dealId")
            if not deal_id:
                continue
            try:
                await self._request("DELETE", f"/positions/{deal_id}")
                logger.info(f"Closed position {deal_id}")
            except Exception as e:
                logger.error(f"Failed to close position {deal_id}: {e}")

    async def close(self) -> None:
        # Best-effort logout, then release the HTTP client.
        try:
            if self.session:
                await self._client.delete(
                    f"{self.base_url}{API_PREFIX}/session",
                    headers=self._get_headers(),
                )
        except Exception:
            pass
        await self._client.aclose()
