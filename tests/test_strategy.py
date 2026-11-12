from dataclasses import replace
from decimal import Decimal as D

from revx import strategy
from revx.market import Bollinger, MarketState
from revx.risk import TrailState
from revx.signals import BollingerDipSignal, make_signal


def state(close=1900, lower=1950, atr=20, pct=50, breakout=False, start=0, open_=None):
    return MarketState(
        symbol="ETH-EUR", candle_start_ms=start, open=D(open_ if open_ is not None else close), high=D(close),
        low=D(close), close=D(close), bands=Bollinger(D(2000), D(2050), D(lower)), atr=D(atr), atr_prev=D(atr),
        realized_vol=D("0.01"), vol_percentile=D(pct), is_breakout=breakout,
    )


def decide(cfg, st=None, price=None, entries=(), breakout=None, pair_open=None, total_open=None, pending=False):
    st = st or state()
    entries = [D(e) for e in entries]
    return strategy.decide_entry(
        state=st, recent_breakout=breakout, signal=make_signal(cfg.strategy),
        price=D(price) if price is not None else st.close, pair_entries=entries,
        pair_open=len(entries) if pair_open is None else pair_open,
        total_open=len(entries) if total_open is None else total_open,
        has_pending_buy=pending, cfg=cfg.strategy,
    )


# ---- signal -------------------------------------------------------------------

def test_dip_signal_fires_below_band_in_normal_vol():
    sig = BollingerDipSignal(D(95))
    assert sig.evaluate(state(close=1900, lower=1950)).fire
    assert not sig.evaluate(state(close=1960, lower=1950)).fire
    extreme = sig.evaluate(state(close=1900, lower=1950, pct=96))
    assert not extreme.fire and extreme.regime == "extreme_vol"


# ---- price band: at most 2 lots within any 10% range ---------------------------

def test_band_allows_two_and_blocks_a_third_within_ten_percent():
    assert strategy.band_ok([], D(2000), D("0.10"), 2)
    assert strategy.band_ok([D(2000)], D(1900), D("0.10"), 2)
    assert not strategy.band_ok([D(2000), D(1900)], D(1850), D("0.10"), 2)


def test_band_is_a_sliding_window_not_fixed_buckets():
    # 1990, 2005 and a new 2010 straddle the 2000 line but sit within 1% of each other.
    assert not strategy.band_ok([D(1990), D(2005)], D(2010), D("0.10"), 2)


def test_band_allows_a_third_lot_more_than_ten_percent_away():
    assert strategy.band_ok([D(2000), D(1900)], D(1700), D("0.10"), 2)
    # 1810 is within 10% of 1900 only (1810 x 1.1 = 1991 < 2000): two lots in that window, allowed.
    assert strategy.band_ok([D(2000), D(1900)], D(1810), D("0.10"), 2)
    # 1820 x 1.1 = 2002 reaches 2000 as well: three lots in one window, blocked.
    assert not strategy.band_ok([D(2000), D(1900)], D(1820), D("0.10"), 2)


# ---- breakout pause -------------------------------------------------------------

def test_breakout_in_window_looks_at_the_last_n_candles_only():
    calm, wild = state(), state(breakout=True)
    assert strategy.breakout_in_window([wild, calm, calm, calm, calm], 4) is None
    assert strategy.breakout_in_window([calm, wild, calm, calm, calm], 4) is wild
    assert strategy.breakout_in_window([calm, calm, calm, calm, wild], 4) is wild   # the breakout candle itself
    assert strategy.breakout_in_window([wild], 0) is None


def test_breakout_pause_blocks_entry_up_or_down(cfg):
    down = state(close=1800, open_=1900, breakout=True)
    up = state(close=2100, open_=2000, breakout=True)
    assert "breakout pause: down" in decide(cfg, breakout=down).reason
    assert "breakout pause: up" in decide(cfg, breakout=up).reason
    assert not decide(cfg, breakout=down).buy


# ---- entry gates ----------------------------------------------------------------

def test_entry_buys_when_every_gate_passes(cfg):
    d = decide(cfg)
    assert d.buy and d.regime == "normal"


def test_entry_gates(cfg):
    assert "not below the lower band" in decide(cfg, state(close=1960)).reason
    assert "back above the lower band" in decide(cfg, price=1955).reason
    assert "already open" in decide(cfg, pending=True).reason
    assert "lot cap" in decide(cfg, total_open=8).reason
    assert "per-pair" in decide(cfg, pair_open=5).reason
    # spacing: latest lot at 1920, ATR 20 -> needs 1890 or lower
    assert "spacing" in decide(cfg, state(close=1895), entries=[1920]).reason
    assert decide(cfg, state(close=1890), entries=[1920]).buy
    # band: two lots already within 10% above
    assert "price band" in decide(cfg, state(close=1850), entries=[2000, 1900]).reason


def test_spacing_uses_the_most_recent_lot(cfg):
    # Oldest first: the latest lot is 1700, so 1900 is not below it by 1.5 ATR.
    assert "spacing" in decide(cfg, state(close=1900), entries=[2200, 1700]).reason


# ---- exits ------------------------------------------------------------------------

def test_advance_lot_applies_new_candles_in_order_and_stops_at_exit():
    trail = TrailState(floor=D(103))
    states = [state(close=c, atr=2, start=i * 10) for i, c in enumerate([100, 104, 120, 113, 200])]
    adv = strategy.advance_lot(trail, None, states, D(3))
    assert adv.armed_now and adv.exit
    assert adv.last_candle_ms == 30                       # stopped at the 113 close
    assert adv.trail.trail_stop == D(114)
    assert adv.exit_state.close == D(113)


def test_advance_lot_skips_candles_already_applied():
    trail = TrailState(floor=D(103))
    states = [state(close=c, atr=2, start=i * 10) for i, c in enumerate([150, 100])]
    adv = strategy.advance_lot(trail, 0, states, D(3))    # the 150 candle predates the lot
    assert not adv.trail.armed and adv.last_candle_ms == 10
    assert strategy.advance_lot(trail, 10, states, D(3)).last_candle_ms == 10
