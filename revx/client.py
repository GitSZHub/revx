"""Signed HTTP client for the Revolut X REST API.

Signature: base64(Ed25519(timestamp_ms + METHOD + path + query + body)), where
path includes the /api prefix and query has no leading "?".

Responses are parsed with Decimal for every number. Money never becomes a float.
"""
from __future__ import annotations

import base64
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlencode

import requests

API_PREFIX = "/api"
MAX_429_RETRIES = 3
MAX_GET_RETRIES = 2
MAX_RETRY_AFTER_SECONDS = 60.0
MIN_REQUEST_INTERVAL_SECONDS = 0.1  # 10 requests/s, the tightest documented per-second limit


class RevxError(Exception):
    """Base class for client errors."""


class RevxNetworkError(RevxError):
    """The request may or may not have reached the exchange."""


class RevxApiError(RevxError):
    def __init__(self, status: int, message: str, error_id: str | None = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.error_id = error_id


class RevxRateLimited(RevxApiError):
    pass


class Signer:
    def __init__(self, private_key: Any):
        self._key = private_key

    @classmethod
    def from_pem_file(cls, path: Path) -> "Signer":
        from cryptography.hazmat.primitives import serialization

        with open(path, "rb") as f:
            return cls(serialization.load_pem_private_key(f.read(), password=None))

    @staticmethod
    def message(timestamp_ms: str, method: str, path: str, query: str = "", body: str = "") -> bytes:
        return (timestamp_ms + method.upper() + path + query + body).encode()

    def sign(self, timestamp_ms: str, method: str, path: str, query: str = "", body: str = "") -> str:
        return base64.b64encode(self._key.sign(self.message(timestamp_ms, method, path, query, body))).decode()


def build_query(params: dict[str, Any] | None) -> str:
    """Query string exactly as sent and signed. None values are dropped, lists are comma-joined."""
    if not params:
        return ""
    items = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        items.append((key, str(value)))
    return urlencode(items, safe=",")


def encode_body(body: dict[str, Any] | None) -> str:
    return "" if body is None else json.dumps(body, separators=(",", ":"))


def parse_json(text: str) -> Any:
    return json.loads(text, parse_float=Decimal, parse_int=_int_or_decimal)


def _int_or_decimal(s: str) -> int:
    return int(s)


def retry_after_seconds(header: str | None) -> float:
    """Retry-After on this API is in milliseconds."""
    try:
        seconds = float(Decimal(header) / 1000) if header else 1.0
    except Exception:
        seconds = 1.0
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


class RevxClient:
    def __init__(
        self,
        api_key: str,
        signer: Signer,
        base_url: str = "https://revx.revolut.com",
        timeout: float = 10,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._api_key = api_key
        self._signer = signer
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._last_request_at = 0.0

    @classmethod
    def from_env(cls, base_url: str, private_key_path: Path, timeout: float = 10) -> "RevxClient":
        from dotenv import load_dotenv

        load_dotenv()
        api_key = os.environ.get("REVX_API_KEY")
        if not api_key:
            raise RevxError("REVX_API_KEY is not set (expected in the environment or in .env)")
        if not Path(private_key_path).is_file():
            raise RevxError(f"private key file not found: {private_key_path}")
        return cls(
            api_key=api_key,
            signer=Signer.from_pem_file(private_key_path),
            base_url=base_url,
            timeout=timeout,
        )

    # ---- transport -------------------------------------------------------

    def _throttle(self) -> None:
        wait = self._last_request_at + MIN_REQUEST_INTERVAL_SECONDS - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last_request_at = self._clock()

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None) -> Any:
        method = method.upper()
        full_path = API_PREFIX + path
        query = build_query(params)
        payload = encode_body(body)
        url = self._base_url + full_path + ("?" + query if query else "")

        rate_limit_retries = 0
        network_retries = 0
        while True:
            self._throttle()
            ts = str(int(self._clock() * 1000))
            headers = {
                "X-Revx-API-Key": self._api_key,
                "X-Revx-Timestamp": ts,
                "X-Revx-Signature": self._signer.sign(ts, method, full_path, query, payload),
            }
            if payload:
                headers["Content-Type"] = "application/json"
            try:
                resp = self._session.request(
                    method, url, headers=headers, data=payload or None, timeout=self._timeout
                )
            except requests.RequestException as exc:
                # Only GETs are retried: a write may have reached the exchange.
                if method == "GET" and network_retries < MAX_GET_RETRIES:
                    network_retries += 1
                    self._sleep(float(network_retries))
                    continue
                raise RevxNetworkError(f"{method} {path}: {type(exc).__name__}") from exc

            if resp.status_code == 429:
                # A 429 was rejected before processing, so retrying is safe for any method.
                if rate_limit_retries < MAX_429_RETRIES:
                    rate_limit_retries += 1
                    self._sleep(retry_after_seconds(resp.headers.get("Retry-After")))
                    continue
                raise RevxRateLimited(429, _error_message(resp), _error_id(resp))

            if resp.status_code >= 500 and method == "GET" and network_retries < MAX_GET_RETRIES:
                network_retries += 1
                self._sleep(float(network_retries))
                continue

            if not resp.ok:
                raise RevxApiError(resp.status_code, _error_message(resp), _error_id(resp))
            if resp.status_code == 204 or not resp.content:
                return None
            return parse_json(resp.text)

    def _paginate(self, path: str, params: dict[str, Any]) -> Iterator[Any]:
        params = dict(params)
        while True:
            page = self._request("GET", path, params)
            yield from page.get("data", [])
            cursor = (page.get("metadata") or {}).get("next_cursor")
            if not cursor:
                return
            params["cursor"] = cursor

    # ---- account and market data (read-only) ------------------------------

    def balances(self) -> Any:
        return self._request("GET", "/1.0/balances")

    def candles(self, symbol: str, interval_minutes: int, since_ms: int | None = None,
                until_ms: int | None = None) -> Any:
        return self._request(
            "GET", f"/1.0/candles/{symbol}",
            {"interval": interval_minutes, "since": since_ms, "until": until_ms},
        )

    def tickers(self, symbols: list[str] | None = None) -> Any:
        return self._request("GET", "/1.0/tickers", {"symbols": symbols} if symbols else None)

    def pairs(self) -> Any:
        return self._request("GET", "/1.0/configuration/pairs")

    def fee_tiers(self) -> Any:
        return self._request("GET", "/1.0/configuration/fees/tiers")

    # ---- orders (read-only) -------------------------------------------------

    def active_orders(self, symbols: list[str] | None = None) -> list[Any]:
        return list(self._paginate("/1.0/orders/active", {"symbols": symbols}))

    def historical_orders(self, start_ms: int, end_ms: int, symbols: list[str] | None = None) -> list[Any]:
        """Date range is at most 30 days per request."""
        return list(self._paginate(
            "/1.0/orders/historical", {"symbols": symbols, "start_date": start_ms, "end_date": end_ms}
        ))

    def get_order(self, venue_order_id: str) -> Any:
        return self._request("GET", f"/1.0/orders/{venue_order_id}")

    def order_fills(self, venue_order_id: str) -> Any:
        return self._request("GET", f"/1.0/orders/fills/{venue_order_id}")

    def private_trades(self, symbol: str, start_ms: int, end_ms: int) -> list[Any]:
        """Date range is at most 30 days per request."""
        return list(self._paginate(
            f"/1.0/trades/private/{symbol}", {"start_date": start_ms, "end_date": end_ms}
        ))

    # ---- order writes: for revx.engine only --------------------------------

    def _post_order(self, body: dict[str, Any]) -> Any:
        """Place an order. Only revx.engine may call this; it enforces the floor guard,
        the dry-run gate and idempotency. Never retried on network errors."""
        return self._request("POST", "/1.0/orders", body=body)

    def _cancel_order(self, venue_order_id: str) -> None:
        self._request("DELETE", f"/1.0/orders/{venue_order_id}")


def _error_message(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("message", resp.text[:200]))
    except Exception:
        return resp.text[:200]


def _error_id(resp: requests.Response) -> str | None:
    try:
        return resp.json().get("error_id")
    except Exception:
        return None
