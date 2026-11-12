"""Entry signals. Pluggable: add a class, register it in SIGNALS, set strategy.signal in config.toml.

A signal only answers "is this closed candle a buy setup?". Caps, spacing, the price-band
rule and the breakout pause live in revx/strategy.py and apply to every signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from revx.config import StrategyConfig
from revx.market import MarketState


@dataclass(frozen=True)
class SignalResult:
    fire: bool
    reason: str
    regime: str     # "normal" or "extreme_vol"


class EntrySignal(Protocol):
    name: str

    def evaluate(self, state: MarketState) -> SignalResult: ...


class BollingerDipSignal:
    """Close below the lower Bollinger band while realized vol is not extreme."""
    name = "bollinger_dip"

    def __init__(self, vol_max_percentile: Decimal):
        self.vol_max_percentile = vol_max_percentile

    def evaluate(self, state: MarketState) -> SignalResult:
        regime = "extreme_vol" if state.vol_percentile > self.vol_max_percentile else "normal"
        if state.close >= state.bands.lower:
            return SignalResult(False, "close is not below the lower band", regime)
        if regime == "extreme_vol":
            return SignalResult(
                False, f"vol percentile {state.vol_percentile:.0f} is above {self.vol_max_percentile}", regime)
        return SignalResult(True, "close below the lower band, vol regime normal", regime)


SIGNALS = {
    "bollinger_dip": lambda cfg: BollingerDipSignal(cfg.vol_max_percentile),
}


def make_signal(cfg: StrategyConfig) -> EntrySignal:
    try:
        return SIGNALS[cfg.signal](cfg)
    except KeyError:
        raise ValueError(f"unknown signal {cfg.signal!r}; known: {sorted(SIGNALS)}") from None
