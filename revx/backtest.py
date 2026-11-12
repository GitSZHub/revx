"""Backtester: historical 4h candles through the same indicator, strategy and risk code as the bot.

Fill model (4h resolution, so it is an approximation):
- A buy fills at the close of the signal candle, at the maker fee. A real post-only order at the
  bid may not fill, so results lean optimistic.
- When a close falls below the trail stop, the lot sells at that close (taker fee) if the close is
  at or above the floor. Otherwise a sell rests at the floor and fills (maker fee) when a later
  candle's high reaches it. No sale is ever below the floor.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from revx import market, risk, strategy
from revx.config import Config
from revx.market import MarketState
from revx.models import Candle, Fees, closed_candles
from revx.risk import TrailState
from revx.signals import EntrySignal

ZERO = Decimal(0)
DAY_MS = 86_400_000
QTY_STEP = Decimal("0.00000001")


@dataclass
class BtLot:
    symbol: str
    entry: Decimal
    qty: Decimal
    cost: Decimal
    opened_ms: int
    trail: TrailState
    last_candle_ms: int
    resting: bool = False      # trail stop hit below the floor: a sell is resting at the floor


@dataclass
class BtTrade:
    symbol: str
    entry: Decimal
    exit_price: Decimal
    floor: Decimal
    opened_ms: int
    closed_ms: int
    pnl: Decimal


@dataclass
class BtResult:
    start_cash: Decimal
    cash: Decimal
    open_value: Decimal
    lowest_equity: Decimal
    trades: list[BtTrade] = field(default_factory=list)
    open_lots: list[BtLot] = field(default_factory=list)
    last_prices: dict[str, Decimal] = field(default_factory=dict)
    skips: dict[str, int] = field(default_factory=dict)
    first_ms: int = 0
    last_ms: int = 0

    @property
    def equity(self) -> Decimal:
        return self.cash + self.open_value

    @property
    def realized(self) -> Decimal:
        return sum((t.pnl for t in self.trades), ZERO)


def run_backtest(series: dict[str, Sequence[MarketState | None]], cfg: Config, fees: Fees,
                 signal: EntrySignal, start_ms: int | None = None) -> BtResult:
    s, ex = cfg.strategy, cfg.exit
    cash = cfg.dry_run_starting_eur
    lots: list[BtLot] = []
    result = BtResult(start_cash=cash, cash=cash, open_value=ZERO, lowest_equity=cash)
    index = {sym: {st.candle_start_ms: i for i, st in enumerate(states) if st is not None}
             for sym, states in series.items()}
    times = sorted({t for sym in index for t in index[sym] if start_ms is None or t >= start_ms})
    if not times:
        return result
    result.first_ms, result.last_ms = times[0], times[-1]

    def sell(lot: BtLot, price: Decimal, fee_rate: Decimal, t: int) -> None:
        nonlocal cash
        if price < lot.trail.floor:
            raise AssertionError(f"backtest tried to sell below floor: {price} < {lot.trail.floor}")
        proceeds = lot.qty * price * (1 - fee_rate)
        cash += proceeds
        lots.remove(lot)
        result.trades.append(BtTrade(lot.symbol, lot.entry, price, lot.trail.floor, lot.opened_ms, t,
                                     proceeds - lot.cost))

    for t in times:
        for sym, states in series.items():
            i = index[sym].get(t)
            if i is None:
                continue
            state = states[i]
            result.last_prices[sym] = state.close

            for lot in [l for l in lots if l.symbol == sym]:
                if lot.resting:
                    if state.high >= lot.trail.floor:
                        sell(lot, lot.trail.floor, fees.maker, t)
                    continue
                adv = strategy.advance_lot(lot.trail, lot.last_candle_ms, [state], ex.trail_atr_mult)
                lot.trail, lot.last_candle_ms = adv.trail, adv.last_candle_ms
                if adv.exit:
                    if state.close >= lot.trail.floor:
                        sell(lot, state.close, fees.taker, t)
                    else:
                        lot.resting = True

            pair_lots = [l for l in lots if l.symbol == sym]
            window = states[max(0, i - s.breakout_cooldown_candles + 1):i + 1]
            decision = strategy.decide_entry(
                state=state, recent_breakout=strategy.breakout_in_window(window, s.breakout_cooldown_candles),
                signal=signal, price=state.close, pair_entries=[l.entry for l in pair_lots],
                pair_open=len(pair_lots), total_open=len(lots), has_pending_buy=False, cfg=s,
            )
            if not decision.buy:
                key = decision.reason.split(":")[0].split(" (")[0]
                result.skips[key] = result.skips.get(key, 0) + 1
                continue
            qty = risk.quantize_down(s.tranche_eur / state.close, QTY_STEP)
            cost = qty * state.close * (1 + fees.maker)
            if cash < cost:
                result.skips["not enough cash"] = result.skips.get("not enough cash", 0) + 1
                continue
            cash -= cost
            floor = risk.floor_price(state.close, fees.maker, fees.taker, ex.min_profit)
            lots.append(BtLot(sym, state.close, qty, cost, t, TrailState(floor=floor), t))

        equity = cash + sum((l.qty * result.last_prices[l.symbol] for l in lots), ZERO)
        result.lowest_equity = min(result.lowest_equity, equity)

    result.cash = cash
    result.open_lots = lots
    result.open_value = sum((l.qty * result.last_prices[l.symbol] for l in lots), ZERO)
    return result


# ---- data -----------------------------------------------------------------------

def fetch_history(client, symbol: str, interval_minutes: int, start_ms: int, now_ms: int,
                  cache_dir: Path) -> list[Candle]:
    """Closed candles from start_ms to now. Cached on disk; only missing candles are fetched."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"candles_{symbol}_{interval_minutes}.json"
    stored: dict[str, dict] = json.loads(cache.read_text()) if cache.exists() else {}
    interval_ms = interval_minutes * 60_000
    since = start_ms
    if stored and min(int(k) for k in stored) <= start_ms + interval_ms:
        since = max(int(k) for k in stored)       # refresh from the newest stored candle
    while since < now_ms:
        until = min(since + 1000 * interval_ms, now_ms)
        for c in client.candles(symbol, interval_minutes, since_ms=since, until_ms=until)["data"]:
            stored[str(c["start"])] = {k: str(v) for k, v in c.items()}
        since = until
        time.sleep(0.3)
    cache.write_text(json.dumps(stored))
    candles = [Candle.from_api(c) for c in stored.values() if int(c["start"]) >= start_ms]
    return closed_candles(candles, interval_minutes, now_ms)


def build_series(symbol: str, candles: Sequence[Candle], cfg: Config) -> list[MarketState | None]:
    s = cfg.strategy
    return market.compute_series(
        symbol, candles, bollinger_period=s.bollinger_period, bollinger_k=s.bollinger_stddev,
        atr_period=s.atr_period, vol_window=s.vol_window, vol_lookback=s.vol_lookback_candles,
        breakout_atr_mult=s.breakout_atr_mult,
    )


# ---- output ---------------------------------------------------------------------

def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def format_result(r: BtResult, show_trades: bool = True) -> str:
    if not r.first_ms:
        return "No candles with full indicator history in the requested period."
    days = (r.last_ms - r.first_ms) / DAY_MS
    buys = len(r.trades) + len(r.open_lots)
    lines = [
        f"Backtest {_day(r.first_ms)} to {_day(r.last_ms)} ({days:.0f} days)",
        f"  start EUR {r.start_cash:.2f} -> equity EUR {r.equity:.2f} "
        f"({(r.equity / r.start_cash - 1) * 100:+.1f}%), lowest equity EUR {r.lowest_equity:.2f}",
        f"  buys {buys} ({buys / days * 30:.1f} per month), closed {len(r.trades)}, open {len(r.open_lots)}",
        f"  realized EUR {r.realized:+.2f}, cash EUR {r.cash:.2f}, open lots worth EUR {r.open_value:.2f}",
    ]
    if r.trades:
        holds = sorted((t.closed_ms - t.opened_ms) / DAY_MS for t in r.trades)
        lines.append(f"  closed lots: median hold {holds[len(holds) // 2]:.0f} days, longest {holds[-1]:.0f} days, "
                     f"sold exactly at the floor: {sum(1 for t in r.trades if t.exit_price == t.floor)}")
    if r.skips:
        lines.append("  candles without a buy, by first failing rule:")
        lines += [f"    {n:>5}  {k}" for k, n in sorted(r.skips.items(), key=lambda kv: -kv[1])]
    if show_trades:
        lines.append("  lots:")
        rows = [(t.opened_ms, f"    {_day(t.opened_ms)} {t.symbol} buy {t.entry:.2f} -> sold {t.exit_price:.2f} "
                              f"on {_day(t.closed_ms)}  {t.pnl:+.2f}") for t in r.trades]
        rows += [(l.opened_ms, f"    {_day(l.opened_ms)} {l.symbol} buy {l.entry:.2f} -> OPEN, now "
                               f"{r.last_prices[l.symbol]:.2f} ({(r.last_prices[l.symbol] / l.entry - 1) * 100:+.1f}%)"
                               f"{', sell resting at floor' if l.resting else ''}") for l in r.open_lots]
        lines += [text for _, text in sorted(rows)]
    return "\n".join(lines)
