"""Backtest the current strategy on each pair separately, each with its own EUR 400 pot.

Run from the repo root:  .venv/Scripts/python research/altcoin_backtest.py
Read-only API calls (candles, fee tiers). Candles are cached in data/.
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from revx import backtest, ledger  # noqa: E402
from revx.config import load_config  # noqa: E402
from revx.models import parse_fees  # noqa: E402
from revx.signals import make_signal  # noqa: E402
from main import make_client  # noqa: E402

PAIRS = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR"]
WANTED_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
HISTORY_START = datetime(2025, 11, 6, tzinfo=timezone.utc)


def main() -> None:
    cfg = load_config()
    client = make_client(cfg)
    fees = parse_fees(client.fee_tiers())
    now = ledger.now_ms()
    s = cfg.strategy
    start = int(WANTED_START.timestamp() * 1000)
    print(f"fees: maker {fees.maker}, taker {fees.taker}; pot EUR {cfg.dry_run_starting_eur} per pair\n")
    series = {}
    for sym in PAIRS:
        candles = backtest.fetch_history(client, sym, s.candle_interval_minutes,
                                         int(HISTORY_START.timestamp() * 1000), now, ROOT / "data")
        series[sym] = backtest.build_series(sym, candles, cfg)
        first = next((x for x in series[sym] if x is not None), None)
        print(f"{sym}: {len(candles)} candles, first with full indicators "
              f"{backtest._day(first.candle_start_ms) if first else 'none'}")
    print()
    for sym in PAIRS:
        one = dataclasses.replace(cfg, strategy=dataclasses.replace(s, pairs=[sym]))
        r = backtest.run_backtest({sym: series[sym]}, one, fees, make_signal(s), start_ms=start)
        print("=" * 70, f"\n{sym}\n" + backtest.format_result(r, show_trades=False))
        first_close = next(x.close for x in series[sym] if x is not None and x.candle_start_ms >= r.first_ms)
        hold = (r.last_prices[sym] / first_close - 1) * 100
        print(f"  buy-and-hold over the same days: {hold:+.1f}%\n")


if __name__ == "__main__":
    main()
