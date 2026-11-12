"""Command line for the Revolut X bot.

  python main.py run                 loop in dry-run (the default)
  python main.py run --live          loop with real orders; also needs DRY_RUN = false in config.toml
  python main.py cycle               one cycle, then exit
  python main.py report              open lots, P&L, equity, progress
  python main.py backtest --days 365
  python main.py deposit EUR 400     record a deposit you made on the exchange
  python main.py withdraw EUR 100
  python main.py resume              clear a halt
  python main.py backup
  python main.py sync --host user@server   pull the server's ledger here (see revx/sync.py)
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from decimal import Decimal, InvalidOperation

from revx import backtest, killswitch, ledger
from revx.client import RevxClient, RevxError
from revx.config import ROOT, Config, load_config, resolve_dry_run
from revx.cycle import Bot, run_cycle
from revx.engine import DryRunBroker, Engine, LiveBroker
from revx.models import parse_fees, parse_tickers
from revx.report import build_report
from revx.signals import make_signal
from revx.sync import SyncError, SyncTarget, refuse_if_synced_copy, sync_from_server

log = logging.getLogger("revx")


def make_client(cfg: Config) -> RevxClient:
    return RevxClient.from_env(cfg.api.base_url, cfg.api.private_key_path, cfg.api.timeout_seconds)


def make_bot(cfg: Config, live_flag: bool, allow_synced_copy: bool = False) -> Bot:
    dry_run, reason = resolve_dry_run(cfg, live_flag)
    log.info("mode: %s", reason)
    client = make_client(cfg)
    conn = ledger.connect(cfg.paths.ledger)
    refuse_if_synced_copy(conn, allow_synced_copy)
    broker = DryRunBroker() if dry_run else LiveBroker(client)
    engine = Engine(conn, broker, dry_run=dry_run, min_profit=cfg.exit.min_profit,
                    daily_order_budget=cfg.api.daily_order_budget,
                    interval_minutes=cfg.strategy.candle_interval_minutes)
    ledger.add_event(conn, "info", "start", reason)
    return Bot(cfg=cfg, conn=conn, client=client, engine=engine, signal=make_signal(cfg.strategy),
               dry_run=dry_run)


def cmd_run(args, cfg: Config) -> int:
    bot = make_bot(cfg, args.live, args.allow_synced_copy)
    interval = args.interval or cfg.loop.interval_seconds
    stop = {"now": False}

    def request_stop(signum, frame):
        stop["now"] = True
        log.info("stop requested; finishing the current cycle")

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    while True:
        log.info("cycle: %s", run_cycle(bot))
        if args.once or stop["now"]:
            break
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline and not stop["now"]:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        if stop["now"]:
            break
    ledger.add_event(bot.conn, "info", "stop", "loop ended")
    bot.conn.close()
    return 0


def cmd_report(args, cfg: Config) -> int:
    conn = ledger.connect(cfg.paths.ledger)
    dry_run = not args.live
    try:
        tickers = parse_tickers(make_client(cfg).tickers(list(cfg.strategy.pairs)))
    except RevxError as exc:
        log.warning("could not fetch prices: %s", exc)
        tickers = None
    print(build_report(conn, dry_run=dry_run, tickers=tickers, pairs=cfg.strategy.pairs,
                       target_multiple=cfg.target_multiple, halt_file=cfg.paths.halt_file))
    return 0


def cmd_backtest(args, cfg: Config) -> int:
    client = make_client(cfg)
    fees = parse_fees(client.fee_tiers())
    now = ledger.now_ms()
    s = cfg.strategy
    interval_ms = s.candle_interval_minutes * 60_000
    start = now - args.days * backtest.DAY_MS
    warmup = (s.vol_window + s.vol_lookback_candles + 5) * interval_ms
    series = {}
    for symbol in s.pairs:
        candles = backtest.fetch_history(client, symbol, s.candle_interval_minutes, start - warmup, now,
                                         ROOT / "data")
        series[symbol] = backtest.build_series(symbol, candles, cfg)
        log.info("%s: %d candles", symbol, len(candles))
    result = backtest.run_backtest(series, cfg, fees, make_signal(s), start_ms=start)
    print(backtest.format_result(result, show_trades=not args.summary))
    return 0


def cmd_cash(args, cfg: Config) -> int:
    try:
        amount = Decimal(args.amount)
    except InvalidOperation:
        print(f"not a number: {args.amount}")
        return 2
    if amount <= 0:
        print("amount must be positive")
        return 2
    conn = ledger.connect(cfg.paths.ledger)
    signed = amount if args.command == "deposit" else -amount
    ledger.add_cash_flow(conn, args.command if args.command == "deposit" else "withdrawal",
                         args.asset.upper(), signed, note=args.note)
    print(f"recorded {args.command} of {amount} {args.asset.upper()} in the live ledger")
    return 0


def cmd_resume(args, cfg: Config) -> int:
    conn = ledger.connect(cfg.paths.ledger)
    reason = killswitch.halt_reason(conn, cfg.paths.halt_file)
    killswitch.resume(conn)
    print(f"cleared: {reason}" if reason else "was not halted")
    if cfg.paths.halt_file.exists():
        print(f"the halt file still exists; delete it to run: {cfg.paths.halt_file}")
    return 0


def cmd_sync(args, cfg: Config) -> int:
    target = SyncTarget.resolve(args.host, args.dir, args.remote_python)
    print(f"pulling the ledger from {target.host}:{target.directory} ...")
    result = sync_from_server(target, cfg.paths.ledger, cfg.paths.backups)
    s = result.summary
    print(f"done: local ledger is now a copy of the server's ({result.remote_file})")
    if result.local_backup:
        print(f"your previous local ledger was kept as {result.local_backup}")
    print(f"  open lots: {s['open_lots_dry_run']} dry-run, {s['open_lots_live']} live; closed lots: {s['closed_lots']}")
    print(f"  orders: {s['orders']}, decisions logged: {s['signals']}, last decision: {s['last_signal']}")
    if s["halted"]:
        print("  the server's bot is HALTED; see `main.py report` for the reason")
    print("view it with: python main.py report")
    return 0


def cmd_backup(args, cfg: Config) -> int:
    conn = ledger.connect(cfg.paths.ledger)
    print(f"backed up to {ledger.backup(conn, cfg.paths.backups)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Revolut X lot-based dip-buying bot")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the decision loop")
    run.add_argument("--live", action="store_true", help="place real orders (also needs DRY_RUN = false)")
    run.add_argument("--interval", type=int, help="seconds between cycles")
    run.add_argument("--once", action="store_true", help="run one cycle and exit")
    run.add_argument("--allow-synced-copy", action="store_true", help="run even on a ledger synced from the server")
    run.set_defaults(func=cmd_run)

    cycle = sub.add_parser("cycle", help="run one cycle and exit")
    cycle.add_argument("--live", action="store_true")
    cycle.add_argument("--allow-synced-copy", action="store_true")
    cycle.set_defaults(func=cmd_run, once=True, interval=None)

    report = sub.add_parser("report", help="show lots, P&L and equity")
    report.add_argument("--live", action="store_true", help="report the live ledger, not the dry-run one")
    report.set_defaults(func=cmd_report)

    bt = sub.add_parser("backtest", help="replay the strategy over historical candles")
    bt.add_argument("--days", type=int, default=365)
    bt.add_argument("--summary", action="store_true", help="omit the per-lot list")
    bt.set_defaults(func=cmd_backtest)

    for name in ("deposit", "withdraw"):
        p = sub.add_parser(name, help=f"record a {name} made on the exchange")
        p.add_argument("asset")
        p.add_argument("amount")
        p.add_argument("--note")
        p.set_defaults(func=cmd_cash)

    sub.add_parser("resume", help="clear a halt").set_defaults(func=cmd_resume)
    sub.add_parser("backup", help="back up the ledger now").set_defaults(func=cmd_backup)

    sync = sub.add_parser("sync", help="pull the server's ledger to this machine")
    sync.add_argument("--host", help="ssh target, e.g. user@server (or set REVX_SYNC_HOST in .env)")
    sync.add_argument("--dir", help="bot folder on the server (default ~/trading/revx, or REVX_SYNC_DIR)")
    sync.add_argument("--remote-python", help="python on the server (default .venv/bin/python, or REVX_SYNC_PYTHON)")
    sync.set_defaults(func=cmd_sync)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    cfg = load_config()
    try:
        return args.func(args, cfg)
    except (RevxError, SyncError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
