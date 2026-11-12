"""Text report: open lots, realized P&L, equity against money put in, progress toward the target."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from revx import killswitch, ledger
from revx.ledger import dec
from revx.models import Ticker, split_symbol

ZERO = Decimal(0)
CENT = Decimal("0.01")


def eur(value: Decimal) -> str:
    return f"{value.quantize(CENT):,}"


def day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def deposited(conn: sqlite3.Connection, dry_run: bool) -> Decimal:
    """EUR put in: EUR cash flows, plus the EUR value of coins at the time they were recorded."""
    total = ZERO
    for r in conn.execute("SELECT asset, amount, eur_value FROM cash_flows WHERE dry_run = ?", (int(dry_run),)):
        total += dec(r["amount"]) if r["asset"] == "EUR" else (dec(r["eur_value"]) or ZERO)
    return total


def build_report(conn: sqlite3.Connection, *, dry_run: bool, tickers: dict[str, Ticker] | None,
                 pairs: tuple[str, ...], target_multiple: Decimal, halt_file: Path) -> str:
    mode = "DRY-RUN (simulated)" if dry_run else "LIVE"
    lines = [f"Revolut X bot report: {mode}", ""]

    reason = killswitch.halt_reason(conn, halt_file)
    lines.append(f"Status: {'HALTED: ' + reason if reason else 'running normally'}")

    held = ledger.holdings(conn, dry_run)
    cash = held.get("EUR", ZERO)
    positions, priced = ZERO, tickers is not None
    if priced:
        for symbol in pairs:
            base, _ = split_symbol(symbol)
            if symbol in tickers:
                positions += held.get(base, ZERO) * tickers[symbol].bid
    else:
        snap = conn.execute("SELECT * FROM equity_snapshots WHERE dry_run = ? ORDER BY ts DESC LIMIT 1",
                            (int(dry_run),)).fetchone()
        if snap:
            cash, positions = dec(snap["eur_cash"]), dec(snap["positions_eur"])
            lines.append(f"(prices unavailable; using the snapshot from {day(snap['ts'])})")
    equity = cash + positions
    put_in = deposited(conn, dry_run)

    realized = sum((dec(r[0]) for r in conn.execute(
        "SELECT realized_pnl_eur FROM disposals WHERE dry_run = ?", (int(dry_run),))), ZERO)
    closed = conn.execute("SELECT COUNT(*) FROM lots WHERE status = 'closed' AND dry_run = ?",
                          (int(dry_run),)).fetchone()[0]

    lines += [
        "",
        f"Money put in:   EUR {eur(put_in)}",
        f"Equity now:     EUR {eur(equity)}  (cash {eur(cash)}, coins {eur(positions)})",
        f"Realized P&L:   EUR {eur(realized)} from {closed} closed lot{'s' if closed != 1 else ''}",
    ]
    if put_in > 0:
        multiple = equity / put_in
        progress = (multiple - 1) / (target_multiple - 1) * 100 if target_multiple > 1 else ZERO
        lines.append(f"Progress:       {multiple:.2f}x of the {target_multiple}x target "
                     f"({progress:.1f}% of the way)")

    lots = ledger.open_lots(conn, dry_run)
    lines += ["", f"Open lots: {len(lots)}"]
    if lots:
        lines.append(f"  {'id':>3} {'pair':8} {'opened':10} {'entry':>10} {'qty':>12} {'floor':>10} "
                     f"{'now':>10} {'P&L EUR':>9} {'P&L %':>7}  state")
    for lot in lots:
        now = tickers[lot.symbol].bid if priced and lot.symbol in tickers else None
        if lot.exit_triggered:
            state = "selling (trail stop hit)"
        elif lot.trail_armed:
            state = f"armed, trail stop {lot.trail_stop}"
        else:
            state = "waiting for floor"
        if now is not None:
            cost_open = lot.cost_basis_eur * lot.qty_open / lot.qty_initial
            pnl = lot.qty_open * now - cost_open
            pnl_txt, pct_txt, now_txt = eur(pnl), f"{pnl / cost_open * 100:+.1f}%", f"{now}"
        else:
            pnl_txt = pct_txt = now_txt = "-"
        lines.append(f"  {lot.id:>3} {lot.symbol:8} {day(lot.opened_at):10} {lot.entry_price:>10.2f} "
                     f"{lot.qty_open:>12.8f} {lot.floor:>10.2f} {now_txt:>10} {pnl_txt:>9} {pct_txt:>7}  {state}")

    working = ledger.active_orders(conn, dry_run)
    lines += ["", f"Working orders: {len(working)}"]
    for o in working:
        lines.append(f"  #{o['id']} {o['side']} {o['symbol']} {o['qty']} @ {o['price']} ({o['state']})")

    unmanaged = []
    for symbol in pairs:
        base, _ = split_symbol(symbol)
        in_lots = sum((lot.qty_open for lot in lots if lot.symbol == symbol), ZERO)
        extra = held.get(base, ZERO) - in_lots
        if extra > 0:
            unmanaged.append(f"{extra} {base}")
    if unmanaged:
        lines += ["", "Held but not managed by the bot (never sold by it): " + ", ".join(unmanaged)]

    problems = conn.execute(
        "SELECT ts, kind, message FROM events WHERE level IN ('warning', 'error') ORDER BY id DESC LIMIT 5"
    ).fetchall()
    if problems:
        lines += ["", "Latest warnings and errors:"]
        lines += [f"  {day(p['ts'])} {p['kind']}: {p['message']}" for p in problems]
    return "\n".join(lines)
