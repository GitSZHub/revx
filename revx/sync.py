"""Pull the server's ledger to this machine.

How it works:
  1. ssh to the server and run `main.py backup` there. That uses SQLite's online backup, so the
     copy is consistent even while the bot is running.
  2. scp the backup file down to a temporary folder.
  3. Check it is a healthy ledger this code can read.
  4. Keep the current local ledger as backups/before-sync-ledger-<time>.db.
  5. Replace the local ledger with the server's and mark it as a synced copy.

A synced copy is for looking at (report, queries). `main.py run` and `main.py cycle` refuse to run on
it unless told otherwise, because a second bot on the same history would split the record.

The server address is never stored in the repo: it comes from the REVX_SYNC_HOST environment
variable (or .env), or the --host option.
"""
from __future__ import annotations

import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from revx import ledger

DEFAULT_DIR = "~/trading/revx"
DEFAULT_PYTHON = ".venv/bin/python"
SSH_OPTS = ["-o", "ConnectTimeout=15"]
REQUIRED_TABLES = {"cash_flows", "lots", "orders", "fills", "disposals", "signals", "events", "config"}
HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:\-\[\]]*$")


class SyncError(RuntimeError):
    pass


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def default_runner(args: list[str], cwd: str | None = None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(args, capture_output=True, text=True, timeout=300, cwd=cwd)


@dataclass(frozen=True)
class SyncTarget:
    host: str
    directory: str = DEFAULT_DIR
    python: str = DEFAULT_PYTHON

    def __post_init__(self) -> None:
        if not HOST_PATTERN.match(self.host):
            raise SyncError(f"not a valid ssh host: {self.host!r}")

    @classmethod
    def resolve(cls, host: str | None = None, directory: str | None = None,
                python: str | None = None) -> "SyncTarget":
        """Options win, then environment variables (read from .env at runtime), then defaults."""
        from dotenv import load_dotenv

        load_dotenv()
        host = host or os.environ.get("REVX_SYNC_HOST")
        if not host:
            raise SyncError("no server given: pass --host user@server or set REVX_SYNC_HOST in .env")
        return cls(
            host=host,
            directory=directory or os.environ.get("REVX_SYNC_DIR") or DEFAULT_DIR,
            python=python or os.environ.get("REVX_SYNC_PYTHON") or DEFAULT_PYTHON,
        )


def remote_command(target: SyncTarget) -> str:
    """Shell command run on the server. A leading ~ is left for the remote shell to expand."""
    d = target.directory
    if d == "~":
        cd = "cd ~"
    elif d.startswith("~/"):
        cd = "cd ~/" + shlex.quote(d[2:])
    else:
        cd = "cd " + shlex.quote(d)
    return f"{cd} && {shlex.quote(target.python)} main.py backup"


@dataclass(frozen=True)
class SyncResult:
    remote_file: str
    local_backup: Path | None
    summary: dict


def sync_from_server(target: SyncTarget, local_ledger: Path, backups_dir: Path, *,
                     run: Runner = default_runner, now: datetime | None = None) -> SyncResult:
    local_ledger, backups_dir = Path(local_ledger), Path(backups_dir)
    ssh = run(["ssh", *SSH_OPTS, target.host, remote_command(target)])
    if ssh.returncode != 0:
        tail = (ssh.stderr or ssh.stdout or "").strip().splitlines()[-3:]
        raise SyncError(f"ssh to {target.host} failed (exit {ssh.returncode}): {' | '.join(tail)}")
    found = re.findall(r"backed up to (.+?)\s*$", ssh.stdout or "", re.M)
    if not found:
        raise SyncError("the server did not report a backup file; is main.py up to date there?")
    remote_file = found[-1]

    with tempfile.TemporaryDirectory() as tmp_dir:
        name = "server-ledger.db"
        scp = run(["scp", "-q", *SSH_OPTS, f"{target.host}:{remote_file}", name], cwd=tmp_dir)
        if scp.returncode != 0:
            tail = (scp.stderr or "").strip().splitlines()[-3:]
            raise SyncError(f"could not download {remote_file}: {' | '.join(tail)}")
        downloaded = Path(tmp_dir) / name
        _validate(downloaded)
        local_backup = _backup_local(local_ledger, backups_dir, now)
        _replace(local_ledger, downloaded)

    conn = ledger.connect(local_ledger)        # also brings an older schema up to date
    try:
        ledger.set_value(conn, "synced_copy_from", target.host)
        ledger.set_value(conn, "synced_at", str(ledger.now_ms()))
        summary = summarize(conn)
    finally:
        conn.close()
    return SyncResult(remote_file, local_backup, summary)


def _validate(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        raise SyncError("the downloaded file is empty")
    try:
        conn = sqlite3.connect(str(path))
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise SyncError(f"the downloaded file is not a readable ledger: {exc}") from exc
    if check != "ok":
        raise SyncError(f"the downloaded ledger failed its integrity check: {check}")
    missing = REQUIRED_TABLES - tables
    if missing:
        raise SyncError(f"the downloaded file is not a bot ledger (missing tables: {sorted(missing)})")
    if version > len(ledger.MIGRATIONS):
        raise SyncError(f"the server's ledger (schema {version}) is newer than this code "
                        f"(schema {len(ledger.MIGRATIONS)}); run `git pull` here first")


def _backup_local(local_ledger: Path, backups_dir: Path, now: datetime | None) -> Path | None:
    """Keep the current local ledger before it is replaced. The name does not match the nightly
    backups' ledger-*.db pattern, so the nightly pruning never deletes it."""
    if not local_ledger.exists() or local_ledger.stat().st_size == 0:
        return None
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / f"before-sync-ledger-{stamp}.db"
    src, dst = sqlite3.connect(str(local_ledger)), sqlite3.connect(str(target))
    try:
        src.backup(dst)
    except sqlite3.Error as exc:
        raise SyncError(f"could not back up the current local ledger: {exc}") from exc
    finally:
        dst.close()
        src.close()
    return target


def _replace(local_ledger: Path, downloaded: Path) -> None:
    """Copy the downloaded ledger over the local one with SQLite's backup API (safe with WAL files)."""
    local_ledger.parent.mkdir(parents=True, exist_ok=True)
    src, dst = sqlite3.connect(str(downloaded)), sqlite3.connect(str(local_ledger))
    try:
        src.backup(dst)
    except sqlite3.Error as exc:
        raise SyncError(f"could not replace the local ledger (is a bot running on it?): {exc}") from exc
    finally:
        dst.close()
        src.close()


def summarize(conn: sqlite3.Connection) -> dict:
    def one(sql: str, *args):
        return conn.execute(sql, args).fetchone()[0]

    last = conn.execute("SELECT MAX(ts) FROM signals").fetchone()[0]
    return {
        "open_lots_dry_run": one("SELECT COUNT(*) FROM lots WHERE status = 'open' AND dry_run = 1"),
        "open_lots_live": one("SELECT COUNT(*) FROM lots WHERE status = 'open' AND dry_run = 0"),
        "closed_lots": one("SELECT COUNT(*) FROM lots WHERE status = 'closed'"),
        "orders": one("SELECT COUNT(*) FROM orders"),
        "signals": one("SELECT COUNT(*) FROM signals"),
        "last_signal": (datetime.fromtimestamp(last / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                        if last else None),
        "halted": ledger.get_value(conn, "halted") == "1",
    }


def refuse_if_synced_copy(conn: sqlite3.Connection, allow: bool) -> None:
    """The bot must not run on a copy of the server's ledger: it would split the history."""
    origin = ledger.get_value(conn, "synced_copy_from")
    if origin and not allow:
        raise SyncError(f"this ledger is a copy synced from {origin}. Running the bot on it would split "
                        f"the history. Use the bot on the server, or pass --allow-synced-copy.")
