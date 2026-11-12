import sqlite3
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from revx import ledger

TABLES = {"cash_flows", "orders", "fills", "lots", "disposals", "equity_snapshots",
          "signals", "events", "config"}


@pytest.fixture
def conn():
    c = ledger.connect(":memory:")
    yield c
    c.close()


def test_schema_has_all_tables_and_migrate_is_idempotent(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert TABLES <= names
    assert ledger.schema_version(conn) == len(ledger.MIGRATIONS)
    assert ledger.migrate(conn) == len(ledger.MIGRATIONS)


def test_floats_are_refused():
    with pytest.raises(TypeError):
        ledger.txt(0.1)
    assert ledger.txt(D("0.10")) == "0.10"
    assert ledger.txt(D("1E-8")) == "0.00000001"
    assert ledger.dec("0.10") == D("0.10")


def test_cash_flow_round_trip(conn):
    ledger.add_cash_flow(conn, "deposit", "EUR", D("400.00"), note="initial")
    row = conn.execute("SELECT * FROM cash_flows").fetchone()
    assert ledger.dec(row["amount"]) == D("400.00") and row["dry_run"] == 0
    with pytest.raises(TypeError):
        ledger.add_cash_flow(conn, "deposit", "EUR", 400.0)


def test_sell_order_requires_a_lot(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO orders (client_order_id, symbol, side, purpose, price, qty, state, created_at, updated_at) "
            "VALUES ('c1', 'ETH-EUR', 'sell', 'exit', '1', '1', 'pending_send', 0, 0)")


def test_client_order_id_is_unique(conn):
    sql = ("INSERT INTO orders (client_order_id, symbol, side, purpose, price, qty, state, created_at, updated_at) "
           "VALUES ('c1', 'ETH-EUR', 'buy', 'entry', '1', '1', 'pending_send', 0, 0)")
    conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_config_values_and_events(conn):
    assert ledger.get_value(conn, "halted") is None
    ledger.set_value(conn, "halted", "1")
    ledger.set_value(conn, "halted", "0")
    assert ledger.get_value(conn, "halted") == "0"
    ledger.add_event(conn, "error", "halt", "reconcile mismatch")
    assert conn.execute("SELECT kind FROM events").fetchone()["kind"] == "halt"


def test_backup_writes_a_readable_copy_and_prunes(tmp_path):
    conn = ledger.connect(tmp_path / "ledger.db")
    ledger.add_cash_flow(conn, "deposit", "EUR", D("400"))
    for day in (1, 2, 3):
        target = ledger.backup(conn, tmp_path / "backups", keep=2,
                               now=datetime(2026, 11, day, tzinfo=timezone.utc))
    conn.close()
    assert [p.name for p in sorted((tmp_path / "backups").iterdir())] == \
        ["ledger-20261102.db", "ledger-20261103.db"]
    copy = sqlite3.connect(str(target))
    assert copy.execute("SELECT amount FROM cash_flows").fetchone()[0] == "400"
    copy.close()
