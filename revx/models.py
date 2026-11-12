"""Plain data types shared across modules. All money and quantities are Decimal."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def to_decimal(value: Any) -> Decimal:
    """Convert an API value (decimal string, int or Decimal) to Decimal. Floats are refused."""
    if isinstance(value, float):
        raise TypeError("float is not allowed for money or quantities")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


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
