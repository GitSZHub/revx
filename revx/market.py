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
    """Everything the strategy needs to know about one closed candle."""
    symbol: str
    candle_start_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    bands: Bollinger
    atr: Decimal
    atr_prev: Decimal          # ATR as it stood before this candle
    realized_vol: Decimal
    vol_percentile: Decimal
    is_breakout: bool          # |close - open| >= breakout_atr_mult x atr_prev


def compute_series(symbol: str, candles: Sequence[Candle], *, bollinger_period: int, bollinger_k: Decimal,
                   atr_period: int, vol_window: int, vol_lookback: int,
                   breakout_atr_mult: Decimal) -> list[MarketState | None]:
    """One MarketState per closed candle (oldest first), in a single pass.

    Entries are None until there is enough history for every indicator. The live loop
    reads the tail; the backtester walks the whole list. Both use this function.
    """
    n = len(candles)
    closes = [c.close for c in candles]
    trs: list[Decimal | None] = [None] + true_ranges(candles)
    rets: list[Decimal | None] = [None] + log_returns(closes)
    atrs: list[Decimal | None] = [None] * n
    vols: list[Decimal | None] = [None] * n
    out: list[MarketState | None] = [None] * n
    first = max(vol_window + vol_lookback - 1, bollinger_period - 1, atr_period + 1)

    for i in range(n):
        if i == atr_period:
            atrs[i] = sma(trs[1:atr_period + 1])
        elif i > atr_period:
            atrs[i] = (atrs[i - 1] * (atr_period - 1) + trs[i]) / atr_period
        if i >= vol_window:
            vols[i] = stddev(rets[i - vol_window + 1:i + 1])
        if i < first:
            continue
        c = candles[i]
        out[i] = MarketState(
            symbol=symbol, candle_start_ms=c.start_ms, open=c.open, high=c.high, low=c.low, close=c.close,
            bands=bollinger(closes[i - bollinger_period + 1:i + 1], bollinger_period, bollinger_k),
            atr=atrs[i], atr_prev=atrs[i - 1],
            realized_vol=vols[i],
            vol_percentile=percentile_rank(vols[i], vols[i - vol_lookback + 1:i + 1]),
            is_breakout=abs(c.close - c.open) >= breakout_atr_mult * atrs[i - 1],
        )
    return out


def candles_needed(vol_window: int, vol_lookback: int) -> int:
    """How many closed candles compute_series needs before its last entry is not None."""
    return vol_window + vol_lookback + 5
