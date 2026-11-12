"""Entry and exit decisions. Pure functions: the live loop and the backtester both call these.

Entry gates, in order (the first that fails is the logged reason):
  1. breakout pause   no new lot while any of the last N closed candles is a breakout
  2. signal           the pluggable entry signal fires on the last closed candle
  3. price check      the price we would pay is still below the lower band
  4. pending order    no buy order already open on this pair
  5. caps             total open lots and open lots per pair
  6. spacing          price at least spacing_atr_mult x ATR below the latest open lot on the pair
  7. price band       at most band_max_lots open lots on the pair within any band_width_pct range
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from revx import risk
from revx.config import StrategyConfig
from revx.market import MarketState
from revx.risk import TrailState
from revx.signals import EntrySignal


@dataclass(frozen=True)
class EntryDecision:
    buy: bool
    reason: str
    regime: str


def breakout_in_window(states: Sequence[MarketState | None], lookback: int) -> MarketState | None:
    """The most recent breakout candle among the last `lookback` closed candles, if any."""
    if lookback <= 0:
        return None
    for s in reversed(states[-lookback:]):
        if s is not None and s.is_breakout:
            return s
    return None


def band_ok(open_entries: Sequence[Decimal], price: Decimal, width_pct: Decimal, max_lots: int) -> bool:
    """True if adding a lot at `price` keeps every price range of width `width_pct` at or under
    `max_lots` lots. The window slides: it is checked from every lot's entry upward."""
    points = sorted([*open_entries, price])
    return all(
        sum(1 for y in points if x <= y <= x * (1 + width_pct)) <= max_lots
        for x in points
    )


def decide_entry(*, state: MarketState, recent_breakout: MarketState | None, signal: EntrySignal,
                 price: Decimal, pair_entries: Sequence[Decimal], pair_open: int, total_open: int,
                 has_pending_buy: bool, cfg: StrategyConfig) -> EntryDecision:
    """`pair_entries` are the pair's open-lot entry prices, oldest first. `pair_open` and
    `total_open` count open lots plus buy orders still working."""
    result = signal.evaluate(state)
    regime = result.regime

    if recent_breakout is not None:
        direction = "up" if recent_breakout.close > recent_breakout.open else "down"
        return EntryDecision(False, f"breakout pause: {direction} breakout candle within the last "
                                    f"{cfg.breakout_cooldown_candles} candles", regime)
    if not result.fire:
        return EntryDecision(False, result.reason, regime)
    if price >= state.bands.lower:
        return EntryDecision(False, "price is back above the lower band", regime)
    if has_pending_buy:
        return EntryDecision(False, "a buy order is already open on this pair", regime)
    if total_open >= cfg.max_open_lots:
        return EntryDecision(False, f"lot cap reached ({cfg.max_open_lots})", regime)
    if pair_open >= cfg.max_open_lots_per_pair:
        return EntryDecision(False, f"per-pair lot cap reached ({cfg.max_open_lots_per_pair})", regime)
    last_entry = pair_entries[-1] if pair_entries else None
    if not risk.spacing_ok(price, last_entry, state.atr, cfg.spacing_atr_mult):
        return EntryDecision(False, f"spacing: price is not {cfg.spacing_atr_mult} x ATR below the "
                                    f"latest lot at {last_entry}", regime)
    if not band_ok(pair_entries, price, cfg.band_width_pct, cfg.band_max_lots):
        return EntryDecision(False, f"price band: already {cfg.band_max_lots} lots within "
                                    f"{cfg.band_width_pct * 100:.0f}% of this price", regime)
    return EntryDecision(True, result.reason, regime)


@dataclass(frozen=True)
class LotAdvance:
    trail: TrailState
    last_candle_ms: int | None
    armed_now: bool
    exit: bool
    exit_state: MarketState | None = None


def advance_lot(trail: TrailState, last_candle_ms: int | None, states: Sequence[MarketState | None],
                atr_mult: Decimal) -> LotAdvance:
    """Apply every closed candle newer than `last_candle_ms` to a lot's trailing stop, in order.
    Stops at the first candle that triggers an exit."""
    armed_now = False
    for s in states:
        if s is None or (last_candle_ms is not None and s.candle_start_ms <= last_candle_ms):
            continue
        update = risk.update_trail(trail, s.close, s.atr, atr_mult)
        trail, last_candle_ms = update.state, s.candle_start_ms
        armed_now = armed_now or update.armed_now
        if update.exit:
            return LotAdvance(trail, last_candle_ms, armed_now, True, s)
    return LotAdvance(trail, last_candle_ms, armed_now, False)
