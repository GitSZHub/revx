# Revolut X spot-trading bot

Lot-based dip-buying bot for BTC-EUR and ETH-EUR on Revolut X, in Python 3.12.

## Hard rules

- NEVER read, print, log or modify `.env`, `.env.*`, `keys/` or `*.pem`. Secrets load only at runtime (python-dotenv, key file path).
- Dry-run is the default. Live orders need `DRY_RUN = false` in `config.toml` AND the `--live` CLI flag (`revx.config.resolve_dry_run`).
- Never sell a lot below its floor. The guard lives in `Engine._submit`, which every order passes through. Sells are limit orders only.
- `Decimal` for all prices, quantities, fees and money. No floats. Decimals are stored in SQLite as TEXT.
- Claude does not run the bot in live mode. Tests, dry-run cycles, backtests and read-only API calls are fine.

## Commands

```
.venv/Scripts/python -m pytest -q             # tests (Windows venv; .venv/bin on Linux)
.venv/Scripts/python main.py cycle            # one dry-run cycle on live prices
.venv/Scripts/python main.py run              # dry-run loop (interval from config.toml)
.venv/Scripts/python main.py report           # dry-run ledger; add --live for the live ledger
.venv/Scripts/python main.py backtest --days 182 [--summary]
.venv/Scripts/python main.py deposit EUR 400  # record a deposit made on the exchange (live ledger)
.venv/Scripts/python main.py resume           # clear a halt
```

Live mode (the user runs this, never Claude): set `DRY_RUN = false`, then `main.py run --live`.

## Layout

- `config.toml`: non-secret settings. `revx/config.py` loads it into frozen dataclasses.
- `revx/client.py`: signed HTTP, rate limiting, 429 handling. Order writes are `_post_order` / `_cancel_order`; only `revx/engine.py` may call them.
- `revx/models.py`: Candle, PairSpec, Ticker, Fees, Lot and response parsers.
- `revx/market.py`: indicators. `compute_series` gives one `MarketState` per closed candle; live and backtest both use it.
- `revx/signals.py`: pluggable entry signal (`strategy.signal` in config). `revx/strategy.py`: entry gates and trail advance, pure functions.
- `revx/risk.py`: floor, trailing stop, spacing, sizing.
- `revx/engine.py`: order lifecycle, floor guard, idempotent placement, `LiveBroker` and `DryRunBroker`, lots and disposals.
- `revx/cycle.py`: one decision cycle. `revx/reconcile.py`, `revx/killswitch.py`.
- `revx/ledger.py`: schema, migrations, queries, backup. `revx/report.py`, `revx/backtest.py`, `main.py` (CLI).
- `deploy/revx-bot.service`: systemd unit template.
- Gitignored runtime files: `ledger.db`, `backups/`, `data/` (candle cache), `HALT`.

## Strategy as built

Entry gates, in order (first failure is logged in `signals.reason`): breakout pause, signal, price still below the band, no working buy on the pair, lot caps, spacing, price band.

- Signal: 4h close below the lower Bollinger band (20, 2) and realized-vol percentile (90 days) at or below 95.
- Breakout pause: no new lot while any of the last 4 closed candles (16h, including the latest) moved at least 2 x ATR open-to-close, up or down. ATR is the value before that candle.
- Price band: at most 2 open lots per pair within any 10% price range (sliding window, not fixed buckets).
- Spacing: at least 1.5 x ATR below the pair's most recent open lot.
- Buys: post-only limit at the bid, cancelled after `unfilled_cancel_minutes` and re-evaluated.
- Exit: floor = entry x (1 + buy fee + taker fee + min_profit), rounded up to the price step. A 4h close at or above the floor arms the trail: max(floor, highest close - 3 x ATR), never moving down. A close below the trail triggers the sale: limit at max(bid, floor), so it rests at the floor if price is below it.
- The high-water mark and exits use 4h closes, not intrabar highs or lows.

## Ledger and safety decisions

- `disposals` (one row per sale of part of a lot) is the tax record: EUR proceeds, fee, cost basis, realized P&L.
- Dry-run and live rows share tables, separated by `dry_run`. An Engine refuses a broker or a lot of the other mode.
- First run records the account's balances as `opening_balance` cash flows. Coins recorded that way belong to no lot and are never sold by the bot (this is how the pre-existing ETH is handled).
- Reconciliation compares live-ledger holdings of EUR and the pair bases with `/balances`. Live: a mismatch halts. Dry-run: it logs a warning only. Deposits must be recorded with `main.py deposit`.
- Halts are sticky (`main.py resume`): halt file, live reconciliation mismatch, `max_consecutive_errors` failed cycles, or a floor-guard violation.
- Idempotency: the order row with its UUID `client_order_id` is committed before sending. Unknown outcomes stay `pending_send`; `recover_pending` looks the ID up on the exchange and either adopts the order or marks it failed. Nothing is re-sent blindly.
- Fills are booked from order-level totals (`filled_quantity`, `average_fill_price`, `total_fee`), not per-trade records.

## Exchange API notes

- Base `https://revx.revolut.com`, paths start `/api/1.0/`. The signature covers the path including `/api`: `timestamp_ms + METHOD + path + query (no "?") + body`, Ed25519, base64.
- URLs and request bodies use `BTC-EUR`; responses use `BTC/EUR` (`models.norm_symbol`).
- `Retry-After` on 429 is in milliseconds. Order placement: 10/s, 1000/day. No sandbox exists.
- Candle history only goes back to 2025-11-06.
- Verified against live responses: balances, pairs, fee tiers, tickers, candles. NOT yet verified: the order placement body and the order-detail fields in `LiveBroker`, because the account has never placed an API order. Check them on the first live order.
- Fees seen: maker 0, taker 0.0009. Minimum order EUR 0.10.

Update this file when a design decision changes.
