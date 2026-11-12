# Revolut X spot-trading bot

Lot-based dip-buying bot for BTC-EUR and ETH-EUR on Revolut X, in Python 3.12.

## Hard rules

- NEVER read, print, log or modify `.env`, `.env.*`, `keys/` or `*.pem`. Secrets load only at runtime (python-dotenv, key file path).
- Dry-run is the default. Live orders need `DRY_RUN = false` in `config.toml` AND the `--live` CLI flag (`revx.config.resolve_dry_run`).
- Never sell a lot below its floor. The guard lives in the order engine, so no code path can bypass it. Sells are limit orders only.
- `Decimal` for all prices, quantities, fees and money. No floats. Decimals are stored in SQLite as TEXT.
- Claude does not run the bot in live mode. Tests, dry-run cycles and read-only API calls are fine.

## Commands

```
.venv/Scripts/python -m pytest -q        # tests (Windows venv; bin/ on Linux)
.venv/Scripts/python test_auth.py        # manual signed-GET smoke test
```

## Layout

- `config.toml`: non-secret settings. `revx/config.py` loads it into frozen dataclasses.
- `revx/`: client, models, market, signals, strategy, risk, engine, ledger, reconcile, killswitch, report, backtest (built in phases; see the plan below).
- `tests/`: pytest.

## Exchange API notes

- Base `https://revx.revolut.com`, paths start `/api/1.0/`. The signature covers the path including the `/api` prefix: `timestamp_ms + METHOD + path + query (no "?") + body`, Ed25519, base64.
- `Retry-After` on 429 is in milliseconds.
- `client_order_id` must be a UUID. There is no lookup by client ID and duplicate handling is undocumented, so idempotency is ours: write the order row before POST, and resolve unknown outcomes by searching active and historical orders.
- Fills carry no fee field; fees come from order details (`total_fee`, `fee_currency`).
- Order placement: 10/s, 1000/day. No sandbox exists.

## Design decisions

- The high-water mark and exits use 4h candle closes, not intrabar highs or lows (`revx/risk.py`).
- The trail stop never moves down, and the floor is rounded up to the price step.
- `disposals` table (one row per sale of part of a lot) is the tax record: EUR proceeds, fee, cost basis, realized P&L.
- Ledger: decimals as TEXT, timestamps as integer ms UTC, schema version in `PRAGMA user_version`.
- Order writes on the client are `_post_order` / `_cancel_order`; only `revx/engine.py` may call them.

## Build phases

0 scaffold, 1 client, 2 ledger, 3 market and risk, 4 strategy, 5 engine, 6 reconcile / kill switches / ETH import, 7 loop and report, 8 backtester, 9 live readiness.

Update this file when a design decision changes.
