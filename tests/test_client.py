import base64
from decimal import Decimal

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from revx.client import (
    RevxApiError,
    RevxClient,
    RevxNetworkError,
    RevxRateLimited,
    Signer,
    build_query,
    encode_body,
    parse_json,
    retry_after_seconds,
)

# Fixed test-only key (RFC 8032 test vector 1 seed). Not a real credential.
SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
KEY = Ed25519PrivateKey.from_private_bytes(SEED)


class FakeResponse:
    def __init__(self, status=200, text="{}", headers=None):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = headers or {}
        self.ok = 200 <= status < 300

    def json(self):
        import json
        return json.loads(self.text)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "data": data})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(responses):
    session = FakeSession(responses)
    sleeps = []
    client = RevxClient("test-key", Signer(KEY), session=session,
                        clock=lambda: 1700000000.0, sleep=sleeps.append)
    return client, session, sleeps


def test_signed_message_layout():
    msg = Signer.message("1700000000000", "get", "/api/1.0/candles/BTC-EUR", "interval=240", "")
    assert msg == b"1700000000000GET/api/1.0/candles/BTC-EUR" + b"interval=240"
    msg = Signer.message("1", "POST", "/api/1.0/orders", "", '{"a":1}')
    assert msg == b'1POST/api/1.0/orders{"a":1}'


def test_signature_verifies_and_is_deterministic():
    signer = Signer(KEY)
    sig = signer.sign("1700000000000", "GET", "/api/1.0/balances")
    assert sig == signer.sign("1700000000000", "GET", "/api/1.0/balances")
    KEY.public_key().verify(base64.b64decode(sig), b"1700000000000GET/api/1.0/balances")


def test_request_signs_exactly_what_is_sent():
    client, session, _ = make_client([FakeResponse(text='{"data":[]}')])
    client.tickers(["BTC-EUR", "ETH-EUR"])
    call = session.calls[0]
    assert call["url"] == "https://revx.revolut.com/api/1.0/tickers?symbols=BTC-EUR,ETH-EUR"
    h = call["headers"]
    assert h["X-Revx-API-Key"] == "test-key"
    assert h["X-Revx-Timestamp"] == "1700000000000"
    KEY.public_key().verify(
        base64.b64decode(h["X-Revx-Signature"]),
        b"1700000000000GET/api/1.0/tickers" + b"symbols=BTC-EUR,ETH-EUR",
    )


def test_post_body_is_signed_as_sent():
    client, session, _ = make_client([FakeResponse(text='{"data":{}}')])
    body = {"client_order_id": "x", "symbol": "BTC-EUR"}
    client._post_order(body)
    call = session.calls[0]
    assert call["data"] == encode_body(body) == '{"client_order_id":"x","symbol":"BTC-EUR"}'
    KEY.public_key().verify(
        base64.b64decode(call["headers"]["X-Revx-Signature"]),
        b"1700000000000POST/api/1.0/orders" + call["data"].encode(),
    )


def test_build_query_drops_none_and_joins_lists():
    assert build_query({"interval": 240, "since": None, "symbols": ["A", "B"]}) == "interval=240&symbols=A,B"
    assert build_query(None) == ""


def test_numbers_parse_as_decimal_never_float():
    data = parse_json('{"price": 0.1, "qty": "0.0728", "n": 3}')
    assert data["price"] == Decimal("0.1") and isinstance(data["price"], Decimal)
    assert data["n"] == 3 and isinstance(data["n"], int)
    assert data["qty"] == "0.0728"


def test_retry_after_is_milliseconds():
    assert retry_after_seconds("5000") == 5.0
    assert retry_after_seconds(None) == 1.0
    assert retry_after_seconds("garbage") == 1.0
    assert retry_after_seconds("99999999") == 60.0


def test_429_waits_retry_after_then_succeeds():
    client, session, sleeps = make_client([
        FakeResponse(429, '{"message":"Rate Limit Exceeded"}', {"Retry-After": "2500"}),
        FakeResponse(text='{"ok":1}'),
    ])
    assert client.balances() == {"ok": 1}
    assert 2.5 in sleeps
    assert len(session.calls) == 2


def test_429_gives_up_after_max_retries():
    client, _, _ = make_client([FakeResponse(429, "{}", {"Retry-After": "1"})] * 4)
    with pytest.raises(RevxRateLimited):
        client.balances()


def test_api_error_carries_status_and_message():
    client, _, _ = make_client([FakeResponse(400, '{"message":"No such pair: BTC-BTC","error_id":"e1"}')])
    with pytest.raises(RevxApiError) as exc:
        client.pairs()
    assert exc.value.status == 400
    assert exc.value.message == "No such pair: BTC-BTC"
    assert exc.value.error_id == "e1"


def test_get_retries_network_error_but_post_does_not():
    client, session, _ = make_client([requests.ConnectionError("x"), FakeResponse(text="[]")])
    assert client.balances() == []
    assert len(session.calls) == 2

    client, session, _ = make_client([requests.Timeout("x"), FakeResponse(text="{}")])
    with pytest.raises(RevxNetworkError):
        client._post_order({"a": 1})
    assert len(session.calls) == 1


def test_pagination_follows_next_cursor():
    client, session, _ = make_client([
        FakeResponse(text='{"data":[{"id":"a"}],"metadata":{"next_cursor":"c1"}}'),
        FakeResponse(text='{"data":[{"id":"b"}],"metadata":{}}'),
    ])
    assert client.active_orders() == [{"id": "a"}, {"id": "b"}]
    assert session.calls[1]["url"].endswith("/api/1.0/orders/active?cursor=c1")
