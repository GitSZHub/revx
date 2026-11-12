"""SQLite ledger: schema, migrations, low-level helpers and backup.

Decimals are stored as TEXT. Timestamps are integer milliseconds UTC.
Rows created in dry-run carry dry_run = 1 and are never mixed into live accounting.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE cash_flows (
        id INTEGER PRIMARY KEY,
        ts INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('deposit', 'withdrawal', 'opening_balance')),
        asset TEXT NOT NULL,
        amount TEXT NOT NULL,              -- signed: withdrawals are negative
        eur_value TEXT,                    -- EUR value at the time, for non-EUR assets
        note TEXT,
        dry_run INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE lots (
        id INTEGER PRIMARY KEY,
        symbol TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('bot', 'import')),
        status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
        entry_price TEXT NOT NULL,
        qty_initial TEXT NOT NULL,
        qty_open TEXT NOT NULL,
        cost_basis_eur TEXT NOT NULL,      -- for qty_initial, including buy fees
        buy_fee_rate TEXT NOT NULL,
        sell_fee_rate TEXT NOT NULL,       -- assumed at entry, used for the floor
        floor TEXT NOT NULL,
        high_water_mark TEXT,
        trail_armed INTEGER NOT NULL DEFAULT 0,
        trail_stop TEXT,
        last_candle_ms INTEGER,            -- last closed candle applied to the trail
        exit_triggered INTEGER NOT NULL DEFAULT 0,  -- a close fell below the trail; sell at >= floor
        exit_price TEXT,                   -- quantity-weighted average over disposals
        realized_pnl_eur TEXT,
        opened_at INTEGER NOT NULL,
        closed_at INTEGER,
        dry_run INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX lots_open ON lots (status, symbol, dry_run);

    CREATE TABLE orders (
        id INTEGER PRIMARY KEY,
        client_order_id TEXT NOT NULL UNIQUE,
        venue_order_id TEXT UNIQUE,
        lot_id INTEGER REFERENCES lots (id),   -- required for sells
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        purpose TEXT NOT NULL CHECK (purpose IN ('entry', 'exit', 'partial_exit')),
        price TEXT NOT NULL,                   -- limit price; all orders are limit orders
        qty TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
            'pending_send', 'open', 'partially_filled', 'filled', 'cancelled', 'rejected', 'failed')),
        filled_qty TEXT NOT NULL DEFAULT '0',
        avg_fill_price TEXT,
        fee TEXT,
        fee_currency TEXT,
        reason TEXT,
        created_at INTEGER NOT NULL,
        sent_at INTEGER,
        updated_at INTEGER NOT NULL,
        dry_run INTEGER NOT NULL DEFAULT 0,
        CHECK (side = 'buy' OR lot_id IS NOT NULL)
    );
    CREATE INDEX orders_state ON orders (state, dry_run);

    CREATE TABLE fills (
        id INTEGER PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders (id),
        venue_trade_id TEXT UNIQUE,
        ts INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        price TEXT NOT NULL,
        qty TEXT NOT NULL,
        quote_amount TEXT NOT NULL,
        fee TEXT NOT NULL,
        fee_currency TEXT NOT NULL,
        is_maker INTEGER,
        dry_run INTEGER NOT NULL DEFAULT 0
    );

    -- One row per sale of (part of) a lot: the tax record.
    CREATE TABLE disposals (
        id INTEGER PRIMARY KEY,
        lot_id INTEGER NOT NULL REFERENCES lots (id),
        order_id INTEGER NOT NULL REFERENCES orders (id),
        ts INTEGER NOT NULL,
        qty TEXT NOT NULL,
        price TEXT NOT NULL,
        proceeds_eur TEXT NOT NULL,        -- gross
        fee_eur TEXT NOT NULL,
        cost_basis_eur TEXT NOT NULL,      -- share of the lot's cost basis, incl. buy fees
        realized_pnl_eur TEXT NOT NULL,    -- proceeds - fee - cost basis
        dry_run INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE equity_snapshots (
        id INTEGER PRIMARY KEY,
        ts INTEGER NOT NULL,
        eur_cash TEXT NOT NULL,
        positions_eur TEXT NOT NULL,
        equity_eur TEXT NOT NULL,
        dry_run INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE signals (
        id INTEGER PRIMARY KEY,
        ts INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        candle_start_ms INTEGER,
        price TEXT,
        atr TEXT,
        realized_vol TEXT,
        vol_percentile TEXT,
        regime TEXT,
        action TEXT NOT NULL,              -- buy, sell, arm, hold, skip
        reason TEXT NOT NULL,
        lot_id INTEGER REFERENCES lots (id),
        dry_run INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        ts INTEGER NOT NULL,
        level TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
        kind TEXT NOT NULL,                -- start, stop, halt, resume, api_error, reconcile, backup, ...
        message TEXT NOT NULL
    );

    CREATE TABLE config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    );
    """,
]


def now_ms() -> int:
    return int(time.time() * 1000)


def txt(value: Any) -> str | None:
    """Decimal (or int) to the TEXT stored in the ledger. Floats are refused."""
    if value is None:
        return None
    if isinstance(value, float):
        raise TypeError("float is not allowed in the ledger; use Decimal")
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError(f"expected Decimal, got {type(value).__name__}")
    return format(Decimal(value), "f")


def dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any migrations not yet applied. Each runs in its own transaction."""
    current = schema_version(conn)
    for number, sql in enumerate(MIGRATIONS, start=1):
        if number <= current:
            continue
        try:
            conn.executescript("BEGIN;\n" + sql + f"\nPRAGMA user_version = {number};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return schema_version(conn)


# ---- small helpers used everywhere ----------------------------------------

def add_event(conn: sqlite3.Connection, level: str, kind: str, message: str, ts: int | None = None) -> None:
    with conn:
        conn.execute("INSERT INTO events (ts, level, kind, message) VALUES (?, ?, ?, ?)",
                     (ts or now_ms(), level, kind, message))


def set_value(conn: sqlite3.Connection, key: str, value: str, ts: int | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, ts or now_ms()),
        )


def get_value(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def add_cash_flow(conn: sqlite3.Connection, kind: str, asset: str, amount: Decimal, *,
                  eur_value: Decimal | None = None, note: str | None = None,
                  dry_run: bool = False, ts: int | None = None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO cash_flows (ts, kind, asset, amount, eur_value, note, dry_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts or now_ms(), kind, asset, txt(amount), txt(eur_value), note, int(dry_run)),
        )
    return cur.lastrowid


# ---- backup -----------------------------------------------------------------

def backup(conn: sqlite3.Connection, backups_dir: Path, *, keep: int = 30,
           now: datetime | None = None) -> Path:
    """Online backup (the sqlite3 .backup API) to backups/ledger-YYYYMMDD.db. Keeps the newest `keep`."""
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    target = backups_dir / f"ledger-{stamp}.db"
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    for old in sorted(backups_dir.glob("ledger-*.db"))[:-keep]:
        old.unlink()
    return target


# ---- lots ---------------------------------------------------------------------

def lot_from_row(r: sqlite3.Row):
    from revx.models import Lot

    return Lot(
        id=r["id"], symbol=r["symbol"], status=r["status"],
        entry_price=dec(r["entry_price"]), qty_initial=dec(r["qty_initial"]), qty_open=dec(r["qty_open"]),
        cost_basis_eur=dec(r["cost_basis_eur"]), floor=dec(r["floor"]),
        trail_armed=bool(r["trail_armed"]), high_water_mark=dec(r["high_water_mark"]),
        trail_stop=dec(r["trail_stop"]), last_candle_ms=r["last_candle_ms"],
        exit_triggered=bool(r["exit_triggered"]), opened_at=r["opened_at"], dry_run=bool(r["dry_run"]),
    )


def get_lot(conn: sqlite3.Connection, lot_id: int):
    row = conn.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
    return lot_from_row(row) if row else None


def open_lots(conn: sqlite3.Connection, dry_run: bool, symbol: str | None = None) -> list:
    """Open lots for one mode, oldest first."""
    sql = "SELECT * FROM lots WHERE status = 'open' AND dry_run = ?"
    args: list[Any] = [int(dry_run)]
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol)
    return [lot_from_row(r) for r in conn.execute(sql + " ORDER BY opened_at, id", args)]


def update_lot_trail(conn: sqlite3.Connection, lot_id: int, trail, last_candle_ms: int | None,
                     exit_triggered: bool) -> None:
    with conn:
        conn.execute(
            "UPDATE lots SET trail_armed = ?, high_water_mark = ?, trail_stop = ?, last_candle_ms = ?, "
            "exit_triggered = ? WHERE id = ?",
            (int(trail.armed), txt(trail.high_water_mark), txt(trail.trail_stop), last_candle_ms,
             int(exit_triggered), lot_id),
        )


# ---- orders -------------------------------------------------------------------

ACTIVE_ORDER_STATES = ("open", "partially_filled")


def active_orders(conn: sqlite3.Connection, dry_run: bool, *, side: str | None = None,
                  symbol: str | None = None, lot_id: int | None = None) -> list[sqlite3.Row]:
    """Orders working on the exchange (or in the simulator), plus any not yet confirmed sent."""
    sql = ("SELECT * FROM orders WHERE dry_run = ? "
           "AND state IN ('pending_send', 'open', 'partially_filled')")
    args: list[Any] = [int(dry_run)]
    for column, value in (("side", side), ("symbol", symbol), ("lot_id", lot_id)):
        if value is not None:
            sql += f" AND {column} = ?"
            args.append(value)
    return list(conn.execute(sql + " ORDER BY id", args))


# ---- signals, holdings, equity ------------------------------------------------

def add_signal(conn: sqlite3.Connection, *, symbol: str, action: str, reason: str, dry_run: bool,
               state=None, price: Decimal | None = None, regime: str | None = None,
               lot_id: int | None = None, ts: int | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO signals (ts, symbol, candle_start_ms, price, atr, realized_vol, vol_percentile, "
            "regime, action, reason, lot_id, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts or now_ms(), symbol,
             state.candle_start_ms if state else None,
             txt(price if price is not None else (state.close if state else None)),
             txt(state.atr) if state else None,
             txt(state.realized_vol) if state else None,
             txt(state.vol_percentile) if state else None,
             regime, action, reason, lot_id, int(dry_run)),
        )


def holdings(conn: sqlite3.Connection, dry_run: bool) -> dict[str, Decimal]:
    """What the ledger says the account holds, per asset: cash flows plus fills minus fees."""
    from revx.models import split_symbol

    out: dict[str, Decimal] = {}

    def add(asset: str, amount: Decimal) -> None:
        out[asset] = out.get(asset, Decimal(0)) + amount

    for r in conn.execute("SELECT asset, amount FROM cash_flows WHERE dry_run = ?", (int(dry_run),)):
        add(r["asset"], dec(r["amount"]))
    for r in conn.execute("SELECT * FROM fills WHERE dry_run = ?", (int(dry_run),)):
        base, quote = split_symbol(r["symbol"])
        sign = 1 if r["side"] == "buy" else -1
        add(base, sign * dec(r["qty"]))
        add(quote, -sign * dec(r["quote_amount"]))
        add(r["fee_currency"], -dec(r["fee"]))
    return out


def add_equity_snapshot(conn: sqlite3.Connection, eur_cash: Decimal, positions_eur: Decimal,
                        dry_run: bool, ts: int | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO equity_snapshots (ts, eur_cash, positions_eur, equity_eur, dry_run) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts or now_ms(), txt(eur_cash), txt(positions_eur), txt(eur_cash + positions_eur), int(dry_run)),
        )
