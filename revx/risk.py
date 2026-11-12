"""Floor, trailing-stop and sizing math. Pure functions, Decimal only.

HWM and exits use 4h candle CLOSES, not intrabar highs or lows.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

ONE = Decimal(1)


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def quantize_up(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def floor_price(entry: Decimal, buy_fee: Decimal, sell_fee: Decimal, min_profit: Decimal,
                price_step: Decimal | None = None) -> Decimal:
    """floor = entry x (1 + buy_fee + sell_fee + min_profit). Rounded UP to the price step,
    so rounding can never lower the floor."""
    value = entry * (ONE + buy_fee + sell_fee + min_profit)
    return quantize_up(value, price_step) if price_step else value


@dataclass(frozen=True)
class TrailState:
    floor: Decimal
    armed: bool = False
    high_water_mark: Decimal | None = None
    trail_stop: Decimal | None = None


@dataclass(frozen=True)
class TrailUpdate:
    state: TrailState
    exit: bool          # this closed candle triggers a sell
    armed_now: bool     # the lot armed on this candle


def update_trail(state: TrailState, close: Decimal, atr: Decimal, atr_mult: Decimal) -> TrailUpdate:
    """Advance a lot's trailing stop by one closed candle.

    - Not armed and close < floor: nothing happens.
    - close >= floor arms the lot. trail = max(floor, hwm - atr_mult x atr); it never moves down.
    - Armed and close < trail (the trail as it stood before this candle): exit.
    """
    if not state.armed:
        if close < state.floor:
            return TrailUpdate(state, exit=False, armed_now=False)
        trail = max(state.floor, close - atr_mult * atr)
        return TrailUpdate(TrailState(state.floor, True, close, trail), exit=False, armed_now=True)

    assert state.trail_stop is not None and state.high_water_mark is not None
    if close < state.trail_stop:
        return TrailUpdate(state, exit=True, armed_now=False)
    hwm = max(state.high_water_mark, close)
    trail = max(state.trail_stop, state.floor, hwm - atr_mult * atr)
    return TrailUpdate(TrailState(state.floor, True, hwm, trail), exit=False, armed_now=False)


def spacing_ok(price: Decimal, last_open_entry: Decimal | None, atr: Decimal, atr_mult: Decimal) -> bool:
    """A new lot on a pair needs price at least atr_mult x ATR below the most recent open lot's entry."""
    if last_open_entry is None:
        return True
    return price <= last_open_entry - atr_mult * atr


def tranche_quantity(tranche_eur: Decimal, price: Decimal, base_step: Decimal,
                     min_order_size: Decimal) -> Decimal | None:
    """Base quantity for one tranche at `price`, rounded down to the step. None if below the minimum."""
    if price <= 0:
        return None
    qty = quantize_down(tranche_eur / price, base_step)
    return qty if qty >= min_order_size and qty > 0 else None
