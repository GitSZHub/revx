"""Shared test fixtures: config, in-memory ledger, synthetic candles, fake API client."""
import math
from decimal import Decimal as D

import pytest

from revx import ledger
from revx.config import load_config
from revx.engine import DryRunBroker, Engine
from revx.models import Fees, PairSpec, Ticker

IV_MS = 240 * 60_000
BASE_MS = 1_700_006_400_000 // IV_MS * IV_MS
N_CANDLES = 600

SPEC = {
    "ETH-EUR": PairSpec("ETH-EUR", "ETH", "EUR", D("0.00000001"), D("0.01"), D("0.00000001"), D("0.1")),
    "BTC-EUR": PairSpec("BTC-EUR", "BTC", "EUR", D("0.00000001"), D("0.01"), D("0.00000001"), D("0.1")),
}
FEES = Fees(D(0), D("0.0009"))


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """Tests must never send a real alert, whatever .env holds."""
    monkeypatch.setattr("revx.notify.send", lambda text: False)
    monkeypatch.setattr("revx.notify.public_ip", lambda: None)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def conn():
    c = ledger.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def clock():
    return Clock(BASE_MS + N_CANDLES * IV_MS + 60_000)   # one minute after the last candle closed


def make_engine(conn, clock, broker=None, dry_run=True, budget=200):
    broker = broker or DryRunBroker()
    engine = Engine(conn, broker, dry_run=dry_run, min_profit=D("0.03"), daily_order_budget=budget,
                    interval_minutes=240, clock=clock)
    engine.pairs, engine.fees = SPEC, FEES
    if isinstance(broker, DryRunBroker):
        broker.fees = FEES
    return engine


def ticker(symbol, bid, ask=None):
    bid = D(str(bid))
    return Ticker(symbol, bid, D(str(ask)) if ask is not None else bid + D("0.5"), bid)


def synthetic_candles(scale=1, dip=True, n=N_CANDLES):
    """Deterministic 4h candles around 2000 x scale: a calm wave, a rough patch in the past (so the
    recent vol percentile is not extreme), and optionally a steady slide below the lower band."""
    closes, price = [], 2000.0
    for i in range(n):
        if 200 <= i < 260:
            price *= 1.015 if i % 2 else 1 / 1.015          # rough patch
        elif dip and i >= n - 6:
            price -= 12                                       # the slide
        else:
            price = 2000 + 20 * math.sin(i / 5)
        closes.append(price)
    out, prev = [], closes[0]
    for i, close in enumerate(closes):
        hi, lo = max(prev, close) + 4, min(prev, close) - 4
        fmt = lambda v: f"{v * scale:.2f}"
        out.append({"start": BASE_MS + i * IV_MS, "open": fmt(prev), "high": fmt(hi), "low": fmt(lo),
                    "close": fmt(close), "volume": "10"})
        prev = close
    return out


class FakeClient:
    """Stands in for RevxClient in cycle tests. Read methods only; no order methods exist."""

    def __init__(self, candles, tickers, balances):
        self.candle_data = candles          # symbol -> list of raw candles
        self.ticker_data = tickers          # symbol -> (bid, ask)
        self.balance_data = balances        # currency -> total string
        self.calls = []

    def pairs(self):
        return {s.replace("-", "/"): {"base": p.base, "quote": p.quote, "base_step": str(p.base_step),
                                      "quote_step": str(p.price_step), "min_order_size": str(p.min_base),
                                      "min_order_size_quote": str(p.min_quote), "status": "active"}
                for s, p in SPEC.items()}

    def fee_tiers(self):
        return {"data": [{"type": "spot", "current_tier": 0, "currency": "USD", "tiers": [
            {"tier": 0, "volume_from": "0", "maker_fee": "0", "taker_fee": "0.0009"}]}]}

    def tickers(self, symbols=None):
        return {"data": [{"symbol": s.replace("-", "/"), "bid": str(b), "ask": str(a), "last_price": str(b)}
                         for s, (b, a) in self.ticker_data.items()]}

    def balances(self):
        return [{"currency": c, "available": v, "reserved": "0", "total": v}
                for c, v in self.balance_data.items()]

    def candles(self, symbol, interval_minutes, since_ms=None, until_ms=None):
        self.calls.append(("candles", symbol))
        return {"data": self.candle_data[symbol]}
