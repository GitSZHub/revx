"""Indicators over closed candles. Pure functions, Decimal only."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from revx.models import Candle

ZERO = Decimal(0)


class NotEnoughData(ValueError):
    pass


def sma(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise NotEnoughData("no values")
    return sum(values, ZERO) / len(values)


def stddev(values: Sequence[Decimal]) -> Decimal:
    """Population standard deviation (the Bollinger convention)."""
    mean = sma(values)
    return (sum(((v - mean) ** 2 for v in values), ZERO) / len(values)).sqrt()


@dataclass(frozen=True)
class Bollinger:
    middle: Decimal
    upper: Decimal
    lower: Decimal


def bollinger(closes: Sequence[Decimal], period: int, k: Decimal) -> Bollinger:
    if len(closes) < period:
        raise NotEnoughData(f"bollinger needs {period} closes, have {len(closes)}")
    window = closes[-period:]
    mid, sd = sma(window), stddev(window)
    return Bollinger(middle=mid, upper=mid + k * sd, lower=mid - k * sd)


def true_ranges(candles: Sequence[Candle]) -> list[Decimal]:
    out = []
    for prev, cur in zip(candles, candles[1:]):
        out.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return out


def atr(candles: Sequence[Candle], period: int) -> Decimal:
    """Wilder's ATR: SMA of the first `period` true ranges, then Wilder smoothing."""
    trs = true_ranges(candles)
    if len(trs) < period:
        raise NotEnoughData(f"atr needs {period + 1} candles, have {len(candles)}")
    value = sma(trs[:period])
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def log_returns(closes: Sequence[Decimal]) -> list[Decimal]:
    return [(b / a).ln() for a, b in zip(closes, closes[1:])]


def realized_vol_series(closes: Sequence[Decimal], window: int) -> list[Decimal]:
    """Rolling standard deviation of log returns, one value per candle once the window is full."""
    rets = log_returns(closes)
    if len(rets) < window:
        raise NotEnoughData(f"realized vol needs {window + 1} closes, have {len(closes)}")
    return [stddev(rets[i - window:i]) for i in range(window, len(rets) + 1)]


def percentile_rank(value: Decimal, history: Sequence[Decimal]) -> Decimal:
    """Percent of `history` at or below `value`, 0 to 100."""
    if not history:
        raise NotEnoughData("no history")
    return Decimal(100) * sum(1 for h in history if h <= value) / len(history)


@dataclass(frozen=True)
class MarketState:
    symbol: str
    candle_start_ms: int      # the last closed candle
    close: Decimal
    bands: Bollinger
    atr: Decimal
    realized_vol: Decimal
    vol_percentile: Decimal


def market_state(symbol: str, candles: Sequence[Candle], *, bollinger_period: int, bollinger_k: Decimal,
                 atr_period: int, vol_window: int, vol_lookback_candles: int) -> MarketState:
    """Compute everything the strategy needs from closed candles (oldest first)."""
    closes = [c.close for c in candles]
    vols = realized_vol_series(closes, vol_window)
    history = vols[-vol_lookback_candles:]
    return MarketState(
        symbol=symbol,
        candle_start_ms=candles[-1].start_ms,
        close=closes[-1],
        bands=bollinger(closes, bollinger_period, bollinger_k),
        atr=atr(candles, atr_period),
        realized_vol=vols[-1],
        vol_percentile=percentile_rank(vols[-1], history),
    )
