"""Plain data types shared across modules. All money and quantities are Decimal.

Symbols are "BTC-EUR" everywhere in this code. The API uses that form in URLs and
request bodies but "BTC/EUR" in responses; norm_symbol() converts.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from revx.risk import TrailState


def to_decimal(value: Any) -> Decimal:
    """Convert an API value (decimal string, int or Decimal) to Decimal. Floats are refused."""
    if isinstance(value, float):
        raise TypeError("float is not allowed for money or quantities")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def norm_symbol(symbol: str) -> str:
    return symbol.replace("/", "-")


def split_symbol(symbol: str) -> tuple[str, str]:
    base, quote = norm_symbol(symbol).split("-")
    return base, quote


@dataclass(frozen=True)
class Candle:
    start_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Candle":
        return cls(
            start_ms=int(raw["start"]),
            open=to_decimal(raw["open"]),
            high=to_decimal(raw["high"]),
            low=to_decimal(raw["low"]),
            close=to_decimal(raw["close"]),
            volume=to_decimal(raw["volume"]),
        )


def closed_candles(candles: list[Candle], interval_minutes: int, now_ms: int) -> list[Candle]:
    """Candles whose interval has fully elapsed, oldest first. Decisions use only these."""
    span = interval_minutes * 60_000
    return sorted((c for c in candles if c.start_ms + span <= now_ms), key=lambda c: c.start_ms)


@dataclass(frozen=True)
class PairSpec:
    symbol: str
    base: str
    quote: str
    base_step: Decimal
    price_step: Decimal
    min_base: Decimal
    min_quote: Decimal


def parse_pairs(raw: Any) -> dict[str, PairSpec]:
    """GET /configuration/pairs returns an object keyed by "BTC/EUR"."""
    out = {}
    for key, p in raw.items():
        if p.get("status", "active") != "active":
            continue
        symbol = norm_symbol(key)
        out[symbol] = PairSpec(
            symbol=symbol, base=p["base"], quote=p["quote"],
            base_step=to_decimal(p["base_step"]), price_step=to_decimal(p["quote_step"]),
            min_base=to_decimal(p["min_order_size"]), min_quote=to_decimal(p["min_order_size_quote"]),
        )
    return out


@dataclass(frozen=True)
class Ticker:
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal


def parse_tickers(raw: Any) -> dict[str, Ticker]:
    out = {}
    for t in raw["data"]:
        symbol = norm_symbol(t["symbol"])
        out[symbol] = Ticker(symbol, to_decimal(t["bid"]), to_decimal(t["ask"]), to_decimal(t["last_price"]))
    return out


@dataclass(frozen=True)
class Fees:
    maker: Decimal
    taker: Decimal


def parse_fees(raw: Any) -> Fees:
    """The spot fee tier the account is currently on."""
    for group in raw["data"]:
        if group["type"] == "spot":
            current = int(group["current_tier"])
            for tier in group["tiers"]:
                if int(tier["tier"]) == current:
                    return Fees(to_decimal(tier["maker_fee"]), to_decimal(tier["taker_fee"]))
    raise ValueError("no spot fee tier in the fee-tiers response")


def parse_balances(raw: Any) -> dict[str, Decimal]:
    """Total (available + reserved) per currency."""
    return {b["currency"]: to_decimal(b["total"]) for b in raw}


@dataclass(frozen=True)
class Lot:
    id: int | None
    symbol: str
    status: str
    entry_price: Decimal
    qty_initial: Decimal
    qty_open: Decimal
    cost_basis_eur: Decimal
    floor: Decimal
    trail_armed: bool
    high_water_mark: Decimal | None
    trail_stop: Decimal | None
    last_candle_ms: int | None
    exit_triggered: bool
    opened_at: int
    dry_run: bool

    @property
    def trail_state(self) -> TrailState:
        return TrailState(self.floor, self.trail_armed, self.high_water_mark, self.trail_stop)
