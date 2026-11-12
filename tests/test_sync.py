import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from revx import ledger
from revx.sync import (SyncError, SyncTarget, refuse_if_synced_copy, remote_command, sync_from_server,
                       summarize)

REMOTE_FILE = "/srv/bot/backups/ledger-20261112.db"


def completed(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class FakeServer:
    """Stands in for ssh and scp. `server_db` is the file the server's backup would produce."""

    def __init__(self, server_db, ssh_code=0, scp_code=0):
        self.server_db, self.ssh_code, self.scp_code = server_db, ssh_code, scp_code
        self.calls = []

    def __call__(self, args, cwd=None):
        self.calls.append(args)
        if args[0] == "ssh":
            if self.ssh_code:
                return completed(self.ssh_code, "", "ssh: connect to host failed")
            return completed(0, f"backed up to {REMOTE_FILE}\n")
        if args[0] == "scp":
            if self.scp_code:
                return completed(self.scp_code, "", "scp: no such file")
            shutil.copy(self.server_db, f"{cwd}/{args[-1]}")
            return completed(0)
        raise AssertionError(args)


def make_ledger(path, lots=0):
    conn = ledger.connect(path)
    ledger.add_cash_flow(conn, "opening_balance", "EUR", D(400), dry_run=True)
    for i in range(lots):
        with conn:
            conn.execute(
                "INSERT INTO lots (symbol, source, status, entry_price, qty_initial, qty_open, cost_basis_eur, "
                "buy_fee_rate, sell_fee_rate, floor, opened_at, dry_run) "
                "VALUES ('ETH-EUR', 'bot', 'open', '2000', '0.025', '0.025', '50', '0', '0.0009', '2061.8', ?, 1)",
                (i,))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return path


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "local.db", tmp_path / "backups", tmp_path / "server.db"


def test_sync_replaces_the_local_ledger_and_keeps_the_old_one(paths):
    local, backups, server = paths
    make_ledger(server, lots=3)
    make_ledger(local, lots=1)
    run = FakeServer(server)
    result = sync_from_server(SyncTarget("me@box"), local, backups, run=run,
                              now=datetime(2026, 11, 12, 21, 0, 5, tzinfo=timezone.utc))

    conn = ledger.connect(local)
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 3
    assert ledger.get_value(conn, "synced_copy_from") == "me@box"
    conn.close()
    assert result.summary["open_lots_dry_run"] == 3 and result.remote_file == REMOTE_FILE

    kept = backups / "before-sync-ledger-20261112-210005.db"
    assert result.local_backup == kept
    old = sqlite3.connect(str(kept))
    assert old.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 1
    old.close()
    assert [c[0] for c in run.calls] == ["ssh", "scp"]
    assert run.calls[0][-2] == "me@box" and run.calls[1][-2] == f"me@box:{REMOTE_FILE}"


def test_first_sync_with_no_local_ledger_needs_no_backup(paths):
    local, backups, server = paths
    make_ledger(server, lots=2)
    result = sync_from_server(SyncTarget("me@box"), local, backups, run=FakeServer(server))
    assert result.local_backup is None and not backups.exists()
    assert result.summary["open_lots_dry_run"] == 2


def test_syncing_twice_keeps_working(paths):
    local, backups, server = paths
    make_ledger(server, lots=2)
    run = FakeServer(server)
    sync_from_server(SyncTarget("me@box"), local, backups, run=run)
    sync_from_server(SyncTarget("me@box"), local, backups, run=run)
    conn = ledger.connect(local)
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 2
    conn.close()


@pytest.mark.parametrize("failing", ["ssh_code", "scp_code"])
def test_failed_transfer_leaves_the_local_ledger_alone(paths, failing):
    local, backups, server = paths
    make_ledger(server, lots=3)
    make_ledger(local, lots=1)
    with pytest.raises(SyncError):
        sync_from_server(SyncTarget("me@box"), local, backups, run=FakeServer(server, **{failing: 255}))
    conn = ledger.connect(local)
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 1
    assert ledger.get_value(conn, "synced_copy_from") is None
    conn.close()


def test_a_file_that_is_not_a_ledger_is_rejected(paths):
    local, backups, server = paths
    make_ledger(local, lots=1)
    server.write_text("this is not a database")
    with pytest.raises(SyncError):
        sync_from_server(SyncTarget("me@box"), local, backups, run=FakeServer(server))
    server.write_bytes(b"")
    with pytest.raises(SyncError):
        sync_from_server(SyncTarget("me@box"), local, backups, run=FakeServer(server))
    other = sqlite3.connect(str(server.with_name("other.db")))
    other.execute("CREATE TABLE unrelated (x)")
    other.commit()
    other.close()
    with pytest.raises(SyncError, match="not a bot ledger"):
        sync_from_server(SyncTarget("me@box"), local, backups, run=FakeServer(server.with_name("other.db")))
    conn = ledger.connect(local)
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 1
    conn.close()


def test_a_ledger_from_newer_code_is_rejected(paths):
    local, backups, server = paths
    make_ledger(server, lots=2)
    conn = sqlite3.connect(str(server))
    conn.execute(f"PRAGMA user_version = {len(ledger.MIGRATIONS) + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(SyncError, match="git pull"):
        sync_from_server(SyncTarget("me@box"), local, backups, run=FakeServer(server))
    assert not local.exists()


def test_server_without_a_backup_line_is_reported(paths):
    local, backups, server = paths
    make_ledger(server)

    def silent(args, cwd=None):
        return completed(0, "") if args[0] == "ssh" else completed(0)
    with pytest.raises(SyncError, match="did not report a backup"):
        sync_from_server(SyncTarget("me@box"), local, backups, run=silent)


# ---- target and command ----------------------------------------------------------------

@pytest.mark.parametrize("bad", ["-oProxyCommand=evil", "", "host name", "a;b", "$(x)"])
def test_unsafe_host_names_are_rejected(bad):
    with pytest.raises(SyncError):
        SyncTarget(bad)


def test_resolve_prefers_options_then_environment(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("REVX_SYNC_HOST", raising=False)
    with pytest.raises(SyncError, match="no server given"):
        SyncTarget.resolve()
    monkeypatch.setenv("REVX_SYNC_HOST", "env@box")
    monkeypatch.setenv("REVX_SYNC_DIR", "/opt/bot")
    t = SyncTarget.resolve()
    assert (t.host, t.directory) == ("env@box", "/opt/bot")
    t = SyncTarget.resolve(host="opt@box", directory="~/x")
    assert (t.host, t.directory) == ("opt@box", "~/x")


def test_remote_command_expands_home_but_quotes_the_rest():
    assert remote_command(SyncTarget("h")) == "cd ~/trading/revx && .venv/bin/python main.py backup"
    assert remote_command(SyncTarget("h", "/opt/my bot")) == "cd '/opt/my bot' && .venv/bin/python main.py backup"
    assert remote_command(SyncTarget("h", "~/a b; rm -rf /")) == \
        "cd ~/'a b; rm -rf /' && .venv/bin/python main.py backup"


# ---- the guard and the summary ----------------------------------------------------------

def test_bot_refuses_to_run_on_a_synced_copy(conn):
    refuse_if_synced_copy(conn, allow=False)                    # a normal ledger is fine
    ledger.set_value(conn, "synced_copy_from", "me@box")
    with pytest.raises(SyncError, match="copy synced from me@box"):
        refuse_if_synced_copy(conn, allow=False)
    refuse_if_synced_copy(conn, allow=True)


def test_summary_counts(conn):
    s = summarize(conn)
    assert s["open_lots_dry_run"] == 0 and s["signals"] == 0 and s["last_signal"] is None
    assert s["halted"] is False
