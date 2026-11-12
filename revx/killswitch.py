"""Kill switches. A halt is sticky: it stays until `main.py resume` clears it.

Halts come from: a halt file on disk, a reconciliation mismatch, repeated errors, or a
floor-guard violation. While halted the bot places no orders.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from revx import ledger


def halt_reason(conn: sqlite3.Connection, halt_file: Path) -> str | None:
    if Path(halt_file).exists():
        return f"halt file present: {halt_file}"
    if ledger.get_value(conn, "halted") == "1":
        return ledger.get_value(conn, "halt_reason") or "halted"
    return None


def halt(conn: sqlite3.Connection, reason: str) -> None:
    ledger.set_value(conn, "halted", "1")
    ledger.set_value(conn, "halt_reason", reason)
    ledger.add_event(conn, "error", "halt", reason)


def resume(conn: sqlite3.Connection) -> None:
    ledger.set_value(conn, "halted", "0")
    ledger.set_value(conn, "error_count", "0")
    ledger.set_value(conn, "halt_noted", "")
    ledger.add_event(conn, "info", "resume", "halt cleared")


def note_halted(conn: sqlite3.Connection, reason: str) -> None:
    """Log a halt-file halt once, not on every cycle."""
    if ledger.get_value(conn, "halt_noted") != reason:
        ledger.set_value(conn, "halt_noted", reason)
        ledger.add_event(conn, "error", "halt", reason)


def record_error(conn: sqlite3.Connection, kind: str, message: str, max_consecutive: int) -> bool:
    """Count a failed cycle. Returns True if this one tripped the halt."""
    count = int(ledger.get_value(conn, "error_count", "0")) + 1
    ledger.set_value(conn, "error_count", str(count))
    ledger.add_event(conn, "warning", kind, f"{message} (consecutive errors: {count})")
    if count >= max_consecutive:
        halt(conn, f"{count} consecutive errors, last: {message}")
        return True
    return False


def record_success(conn: sqlite3.Connection) -> None:
    if ledger.get_value(conn, "error_count", "0") != "0":
        ledger.set_value(conn, "error_count", "0")
