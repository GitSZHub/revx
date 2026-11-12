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
.venv/Scripts/python main.py sync             # pull the server's ledger here (needs REVX_SYNC_HOST, see below)
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
- `research/`: write-ups and scripts for studies (index in `research/README.md`). New findings go there first.
- `revx/sync.py`: pulls the server's ledger to this machine (`main.py sync`).
- `deploy/revx-bot.service`: systemd unit template.
- Gitignored runtime files: `ledger.db`, `backups/`, `data/` (candle cache), `HALT`.

## Strategy as built

Entry gates, in order (first failure is logged in `signals.reason`): breakout pause, signal, price still below the band, no working buy on the pair, lot caps, spacing, price band.

- Signal: 4h close below the lower Bollinger band (20, 2) and realized-vol percentile (90 days) at or below 95.
- Breakout pause: no new lot while any of the last 8 closed candles (32h, including the latest) moved at least 2 x ATR open-to-close, up or down. ATR is the value before that candle.
- Price band: at most 2 open lots per pair within any 15% price range (sliding window, not fixed buckets).
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

## Syncing the server's ledger

The bot runs on a server; `main.py sync` copies the server's ledger to this machine so `main.py report` can show it.

- It runs `main.py backup` on the server over ssh (SQLite online backup, safe while the bot runs), downloads that file with scp, validates it, keeps the previous local ledger as `backups/before-sync-ledger-<time>.db`, then replaces the local ledger.
- Server address: `--host user@server` or `REVX_SYNC_HOST` in `.env`. Optional: `REVX_SYNC_DIR` (default `~/trading/revx`), `REVX_SYNC_PYTHON` (default `.venv/bin/python`). The address is never committed to the repo.
- The result is marked as a synced copy (`synced_copy_from` in the ledger `config` table). `main.py run` and `main.py cycle` refuse to run on it unless `--allow-synced-copy`, because a second bot on the same history would split the record.
- It refuses a server ledger from newer code (run `git pull` here first), a file that is not a bot ledger, or any failed transfer; the local ledger is untouched in those cases.

## Decisions and test results

All from backtests on real 4h candles (Revolut X history starts 2025-11-06; indicators need about 3 months, so usable data starts 2026-02-07). Numbers are for the last 6 months unless stated. Re-open a decision only with new evidence; the sample is small and mostly a falling market.

- Adopted: breakout pause 32h (8 candles), up from 16h. 6 months: +16.2% against +11.1%; better in all three windows tried.
- Adopted: price band 15% (2 open lots per pair within any 15% range), up from 10%. A wider band is stricter. BTC+ETH shared pot: +23.9% against +21.3% (all), +18.2% against +16.2% (6 months), +2.0% against +1.2% (90 days). 20% was worse (+18.7%, +11.8%). Small gain, one market; it would hurt SOL and XRP (about -5 points) if those pairs are ever added. See `research/2026-11-12-band-width.md`.
- Rejected, fewer lots (4 total, 3 per pair, 1 per price range): smaller October loss but profit fell from +11.1% to +3.6..+7.8%.
- Rejected, waiting for +10% before selling: +3.6% (fixed target) and +8.6% (10% floor then trail) against +14.0%; money tied up for months.
- Rejected, faster exits (trail 2 or 1.5 x ATR, floor 1.5 or 2%, selling when the live price touches the trail): helped October a little, cut 6-month profit to +5..+14%.
- Tested, trail wider than 3 x ATR (4, 5, 6, 8): no steady trend (+12..+17%), so noise. Kept 3.
- Rejected, 8h / 12h / 1 day candles: +9.6%, +7.5%, +6.2% against +19.7% on 4h over 23 Feb to 12 Nov. Revolut X serves only 4h and 1d candles; 8h and 12h would need aggregating 4h.
- Not adopted yet: spacing 4 x ATR (+12.8% against +11.1%, tested with the old 16h pause).
- Rejected, buying upside breakouts: added almost nothing.
- Rejected idea, a trend filter or partial sells at the floor: the user's reasoning is that no one can know a trend while it is happening, and our own data agrees (the best trades were dips bought in the May-June fall). Do not build rules that depend on predicting trend. `exit.partial_pct` exists in the code but the backtester does not model it.
- Behaviour: in falling months the bot mostly buys (23 buys, 9 sells over 5 months); in rising months it mostly sells (18 buys, 27 sells). In a strong rally it only roughly keeps pace with holding (August: bot +24.5%, BTC +24.0%, ETH +31.6%) and sells some lots at the +3% floor before further rises.
- Known weakness: a sale triggered while price is below the floor rests at the floor and may not fill for weeks (BTC bought 4 Nov at 67,943, floor 70,042, fell below it in one candle).
- Backtest fills assume buys fill at the candle close, so real results will probably be somewhat worse. Dry-run fills are stricter than live (the simulator waits for the ask to reach the bid).

## Status (12 Nov 2026)

- Built, tested (98 tests), pushed to `origin/bot-build`. Not merged into `main`.
- Running in dry-run as a systemd service on the user's server, with the 32h pause once pulled and restarted there.
- The user has not yet deposited the EUR 400 (the account holds 0.0728 ETH and no EUR). The existing ETH belongs to no lot and is never sold by the bot.
- Unproven: the order placement body and order-detail field names in `LiveBroker`; confirm them on the first live order.
- Next: let the dry-run collect decisions for several days, then deposit, record it with `main.py deposit EUR 400`, set `DRY_RUN = false` and start with `--live` (the user does this, not Claude).

## Exchange API notes

- Base `https://revx.revolut.com`, paths start `/api/1.0/`. The signature covers the path including `/api`: `timestamp_ms + METHOD + path + query (no "?") + body`, Ed25519, base64.
- URLs and request bodies use `BTC-EUR`; responses use `BTC/EUR` (`models.norm_symbol`).
- `Retry-After` on 429 is in milliseconds. Order placement: 10/s, 1000/day. No sandbox exists.
- Candle history only goes back to 2025-11-06.
- Verified against live responses: balances, pairs, fee tiers, tickers, candles. NOT yet verified: the order placement body and the order-detail fields in `LiveBroker`, because the account has never placed an API order. Check them on the first live order.
- Fees seen: maker 0, taker 0.0009. Minimum order EUR 0.10.

Update this file when a design decision changes.
