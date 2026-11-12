from decimal import Decimal as D

import pytest

from revx import market, risk
from revx.models import Candle, closed_candles, to_decimal
from revx.risk import TrailState, update_trail


def candle(start, h, l, c):
    return Candle(start_ms=start, open=D(c), high=D(h), low=D(l), close=D(c), volume=D(1))


def approx(a, b, tol="0.0000001"):
    return abs(a - b) < D(tol)


# ---- models -----------------------------------------------------------------

def test_to_decimal_refuses_float():
    assert to_decimal("0.0728") == D("0.0728")
    with pytest.raises(TypeError):
        to_decimal(0.1)


def test_closed_candles_drops_the_forming_candle():
    cs = [candle(0, 1, 1, 1), candle(240 * 60_000, 1, 1, 1)]
    assert len(closed_candles(cs, 240, now_ms=240 * 60_000 + 5)) == 1
    assert len(closed_candles(cs, 240, now_ms=480 * 60_000)) == 2


# ---- indicators -------------------------------------------------------------

def test_bollinger_hand_computed():
    b = market.bollinger([D(x) for x in (9, 1, 2, 3, 4, 5)], period=5, k=D(2))
    assert b.middle == D(3)
    assert approx(b.lower, D(3) - 2 * D(2).sqrt())
    assert approx(b.upper, D(3) + 2 * D(2).sqrt())


def test_atr_wilder_hand_computed():
    cs = [candle(0, 10, 8, 9), candle(1, 11, 9, 10), candle(2, 13, 10, 12), candle(3, 12, 9, 10)]
    assert market.true_ranges(cs) == [D(2), D(3), D(3)]
    assert market.atr(cs, 2) == D("2.75")   # sma(2, 3) = 2.5, then (2.5 + 3) / 2


def test_not_enough_data_raises():
    with pytest.raises(market.NotEnoughData):
        market.bollinger([D(1)], 20, D(2))
    with pytest.raises(market.NotEnoughData):
        market.atr([candle(0, 1, 1, 1)], 14)


def test_realized_vol_and_percentile():
    flat = [D(100)] * 6
    assert market.realized_vol_series(flat, 3) == [D(0)] * 3
    assert market.percentile_rank(D(5), [D(1), D(5), D(9), D(10)]) == D(50)
    assert market.percentile_rank(D(11), [D(1), D(5)]) == D(100)


# ---- floor ------------------------------------------------------------------

def test_floor_price_formula():
    assert risk.floor_price(D(2000), D("0.0009"), D("0.0009"), D("0.03")) == D("2063.6000")


def test_floor_rounds_up_never_down():
    f = risk.floor_price(D("1999.99"), D("0.0009"), D("0.0009"), D("0.03"), price_step=D("0.01"))
    exact = D("1999.99") * D("1.0318")
    assert f >= exact and f - exact < D("0.01")
    assert f == D("2063.59")


# ---- trailing stop ------------------------------------------------------------

def test_trail_lifecycle():
    atr, mult = D(2), D(3)
    s = TrailState(floor=D(103))

    u = update_trail(s, D(100), atr, mult)              # below floor: nothing
    assert not u.state.armed and not u.exit and not u.armed_now

    u = update_trail(u.state, D(104), atr, mult)        # arms; trail clamps to the floor
    assert u.armed_now and u.state.armed and not u.exit
    assert u.state.high_water_mark == D(104) and u.state.trail_stop == D(103)

    u = update_trail(u.state, D(120), atr, mult)        # new high: trail = 120 - 6
    assert u.state.high_water_mark == D(120) and u.state.trail_stop == D(114)

    u = update_trail(u.state, D(118), atr, mult)        # pullback above the trail: hold
    assert not u.exit and u.state.trail_stop == D(114)

    u = update_trail(u.state, D("113.99"), atr, mult)   # close below the trail: exit
    assert u.exit


def test_trail_never_moves_down_when_atr_grows():
    s = TrailState(floor=D(103), armed=True, high_water_mark=D(120), trail_stop=D(114))
    u = update_trail(s, D(119), D(10), D(3))            # 120 - 30 = 90 would be lower
    assert u.state.trail_stop == D(114)


def test_trail_stop_is_never_below_floor():
    s = TrailState(floor=D(103))
    for close in (D(103), D(105), D(104), D(110)):
        u = update_trail(s, close, D(50), D(3))
        s = u.state
        assert s.trail_stop >= s.floor


def test_close_exactly_at_trail_does_not_exit():
    s = TrailState(floor=D(103), armed=True, high_water_mark=D(120), trail_stop=D(114))
    assert not update_trail(s, D(114), D(2), D(3)).exit


# ---- spacing and sizing -----------------------------------------------------

def test_spacing():
    assert risk.spacing_ok(D(100), None, D(2), D("1.5"))
    assert risk.spacing_ok(D(97), D(100), D(2), D("1.5"))
    assert not risk.spacing_ok(D("97.01"), D(100), D(2), D("1.5"))


def test_tranche_quantity_rounds_down_and_respects_minimum():
    assert risk.tranche_quantity(D(50), D(3000), D("0.0001"), D("0.0001")) == D("0.0166")
    assert risk.tranche_quantity(D(50), D(90000), D("0.001"), D("0.001")) is None
