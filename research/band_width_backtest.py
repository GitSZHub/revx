"""Compare price-band width (max 2 open lots within any X% price range) on several windows.

Run from the repo root:  .venv/Scripts/python research/band_width_backtest.py
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
WIDTHS = ["0.10", "0.15", "0.20"]
HIST = int(datetime(2025, 11, 6, tzinfo=timezone.utc).timestamp() * 1000)


def main() -> None:
    cfg = load_config()
    client = make_client(cfg)
    fees = parse_fees(client.fee_tiers())
    now = ledger.now_ms()
    s = cfg.strategy
    series = {}
    for sym in PAIRS:
        c = backtest.fetch_history(client, sym, s.candle_interval_minutes, HIST, now, ROOT / "data")
        series[sym] = backtest.build_series(sym, c, cfg)
    windows = {"all (from 2026-02-07)": 0, "last 182 days": now - 182 * backtest.DAY_MS,
               "last 90 days": now - 90 * backtest.DAY_MS}
    setups = {"BTC+ETH shared pot (live config)": ["BTC-EUR", "ETH-EUR"]} | {f"{p} alone": [p] for p in PAIRS}
    for wname, start in windows.items():
        print(f"\n=== {wname} ===")
        print(f"{'setup':34}{'width':>6}{'equity':>9}{'ret':>8}{'low':>9}{'buys':>6}{'closed':>7}{'open':>5}")
        for setup, pairs in setups.items():
            for w in WIDTHS:
                c2 = dataclasses.replace(cfg, strategy=dataclasses.replace(s, pairs=pairs, band_width_pct=Decimal(w)))
                r = backtest.run_backtest({p: series[p] for p in pairs}, c2, fees, make_signal(c2.strategy), start_ms=start)
                print(f"{setup:34}{w:>6}{r.equity:9.2f}{(r.equity / r.start_cash - 1) * 100:+7.1f}%"
                      f"{r.lowest_equity:9.2f}{len(r.trades) + len(r.open_lots):6}{len(r.trades):7}{len(r.open_lots):5}")


if __name__ == "__main__":
    main()
