from dataclasses import replace
from decimal import Decimal as D

import pytest

from conftest import FakeClient, IV_MS, make_engine, synthetic_candles
from revx import killswitch, ledger, reconcile
from revx.cycle import Bot, run_cycle
from revx.signals import make_signal
from test_engine import FakeLiveBroker

BALANCES = {"EUR": "0.00", "ETH": "0.07283218", "BTC": "0.00000000"}


def make_bot(cfg, conn, clock, tmp_path, *, dry_run=True, eth_dip=True, balances=None):
    cfg = replace(cfg, paths=replace(cfg.paths, halt_file=tmp_path / "HALT", backups=tmp_path / "backups"))
    client = FakeClient(
        candles={"ETH-EUR": synthetic_candles(dip=eth_dip), "BTC-EUR": synthetic_candles(scale=30, dip=False)},
        tickers={"ETH-EUR": (D("1913.00"), D("1913.50")), "BTC-EUR": (D("60240.00"), D("60250.00"))},
        balances=dict(balances or BALANCES),
    )
    broker = None if dry_run else FakeLiveBroker(conn)
    engine = make_engine(conn, clock, broker=broker, dry_run=dry_run)
    return Bot(cfg=cfg, conn=conn, client=client, engine=engine, signal=make_signal(cfg.strategy),
               dry_run=dry_run, clock=clock)


def rows(conn, sql, *args):
    return conn.execute(sql, args).fetchall()


def test_dry_run_cycle_places_a_simulated_buy_and_logs_every_decision(cfg, conn, clock, tmp_path):
    bot = make_bot(cfg, conn, clock, tmp_path)
    status = run_cycle(bot)
    assert "ETH-EUR buy" in status and "BTC-EUR skip" in status

    orders = rows(conn, "SELECT * FROM orders")
    assert len(orders) == 1
    o = orders[0]
    assert (o["symbol"], o["side"], o["state"], o["dry_run"]) == ("ETH-EUR", "buy", "open", 1)
    assert o["price"] == "1913.00"
    assert D("49.9") < D(o["qty"]) * D(o["price"]) <= D(50)

    signals = {r["symbol"]: r for r in rows(conn, "SELECT * FROM signals")}
    assert signals["ETH-EUR"]["action"] == "buy" and signals["ETH-EUR"]["atr"] is not None
    assert signals["BTC-EUR"]["action"] == "skip"
    assert "not below the lower band" in signals["BTC-EUR"]["reason"]

    # The real account's holdings were recorded as opening balances, outside any lot.
    opening = {r["asset"]: r for r in rows(conn, "SELECT * FROM cash_flows WHERE dry_run = 0")}
    assert set(opening) == {"ETH"} and opening["ETH"]["amount"] == "0.07283218"
    assert rows(conn, "SELECT amount FROM cash_flows WHERE dry_run = 1")[0]["amount"] == "400"
    assert len(rows(conn, "SELECT * FROM equity_snapshots WHERE dry_run = 1")) == 1


def test_simulated_buy_fills_and_no_second_lot_is_stacked(cfg, conn, clock, tmp_path):
    bot = make_bot(cfg, conn, clock, tmp_path)
    run_cycle(bot)
    clock.now += 300_000
    bot.client.ticker_data["ETH-EUR"] = (D("1912.00"), D("1913.00"))    # ask reaches our bid
    status = run_cycle(bot)

    lots = ledger.open_lots(conn, dry_run=True)
    assert len(lots) == 1 and lots[0].entry_price == D("1913.00") and lots[0].floor == D("1972.12")
    assert "spacing" in status                                           # 1912 is not 1.5 ATR below 1913
    assert len(rows(conn, "SELECT * FROM orders")) == 1
    assert ledger.open_lots(conn, dry_run=False) == []
    assert bot.client.calls.count(("candles", "ETH-EUR")) == 1           # series cached within a candle


def test_exit_sells_at_or_above_floor_after_a_close_below_the_trail(cfg, conn, clock, tmp_path):
    bot = make_bot(cfg, conn, clock, tmp_path)
    run_cycle(bot)
    clock.now += 300_000
    bot.client.ticker_data["ETH-EUR"] = (D("1912.00"), D("1913.00"))
    run_cycle(bot)
    lot = ledger.open_lots(conn, dry_run=True)[0]

    def add_candle(close):
        data = bot.client.candle_data["ETH-EUR"]
        prev = data[-1]
        data.append({"start": prev["start"] + IV_MS, "open": prev["close"], "high": str(max(D(prev["close"]), D(close)) + 4),
                     "low": str(min(D(prev["close"]), D(close)) - 4), "close": str(close), "volume": "10"})
        clock.now += IV_MS

    for close in (1950, 1990, 2080):                 # recovery; 1990 arms the lot (floor 1972.12)
        add_candle(close)
        bot.client.ticker_data["ETH-EUR"] = (D(close), D(close) + 1)
        run_cycle(bot)
    armed = ledger.get_lot(conn, lot.id)
    assert armed.trail_armed and armed.trail_stop >= armed.floor and not armed.exit_triggered

    add_candle(1975)                                 # closes below the trail, bid below it too
    bot.client.ticker_data["ETH-EUR"] = (D("1960.00"), D("1961.00"))
    run_cycle(bot)
    triggered = ledger.get_lot(conn, lot.id)
    assert triggered.exit_triggered
    sell = rows(conn, "SELECT * FROM orders WHERE side = 'sell'")[0]
    assert D(sell["price"]) == triggered.floor       # bid 1960 is below the floor: rest at the floor
    assert sell["state"] == "open"

    clock.now += 300_000
    bot.client.ticker_data["ETH-EUR"] = (D("1980.00"), D("1981.00"))   # bid back above the floor
    run_cycle(bot)
    done = ledger.get_lot(conn, lot.id)
    assert done.status == "closed"
    d = rows(conn, "SELECT * FROM disposals")[0]
    assert D(d["price"]) >= triggered.floor and D(d["realized_pnl_eur"]) > D("1.4")
    assert any(r["action"] == "arm" for r in rows(conn, "SELECT action FROM signals"))
    assert any(r["action"] == "sell" for r in rows(conn, "SELECT action FROM signals"))


def test_halt_file_stops_all_order_placement(cfg, conn, clock, tmp_path):
    bot = make_bot(cfg, conn, clock, tmp_path)
    bot.cfg.paths.halt_file.write_text("stop")
    assert run_cycle(bot).startswith("halted")
    assert run_cycle(bot).startswith("halted")
    assert rows(conn, "SELECT * FROM orders") == []
    assert len(rows(conn, "SELECT * FROM events WHERE kind = 'halt'")) == 1     # logged once
    bot.cfg.paths.halt_file.unlink()
    assert "buy" in run_cycle(bot)


def test_live_reconciliation_mismatch_halts_and_places_nothing(cfg, conn, clock, tmp_path):
    bot = make_bot(cfg, conn, clock, tmp_path, dry_run=False, balances={**BALANCES, "EUR": "400.00"})
    ledger.add_cash_flow(conn, "opening_balance", "EUR", D("400"))
    ledger.add_cash_flow(conn, "opening_balance", "ETH", D("0.07283218"))
    bot.client.balance_data["ETH"] = "0.05000000"            # coins went missing
    status = run_cycle(bot)
    assert status.startswith("halted: reconciliation mismatch: ETH")
    assert rows(conn, "SELECT * FROM orders") == []
    assert killswitch.halt_reason(conn, bot.cfg.paths.halt_file)

    bot.client.balance_data["ETH"] = "0.07283218"
    assert run_cycle(bot).startswith("halted")               # sticky until resumed
    killswitch.resume(conn)
    assert "buy" in run_cycle(bot)
    live_order = rows(conn, "SELECT * FROM orders")[0]
    assert live_order["dry_run"] == 0 and len(bot.engine.broker.submits) == 1


def test_dry_run_mismatch_warns_but_keeps_running(cfg, conn, clock, tmp_path):
    bot = make_bot(cfg, conn, clock, tmp_path)
    run_cycle(bot)
    bot.client.balance_data["EUR"] = "400.00"                # a deposit the ledger does not know about
    clock.now += 300_000
    assert not run_cycle(bot).startswith("halted")
    assert len(rows(conn, "SELECT * FROM events WHERE kind = 'reconcile'")) == 1


def test_repeated_api_errors_halt(cfg, conn, clock, tmp_path):
    from revx.client import RevxApiError
    bot = make_bot(cfg, conn, clock, tmp_path)

    def boom(*a, **k):
        raise RevxApiError(500, "Something went wrong!")
    bot.client.fee_tiers = boom
    for _ in range(cfg.api.max_consecutive_errors - 1):
        assert run_cycle(bot).startswith("api error:")
    assert "halted" in run_cycle(bot)
    assert run_cycle(bot).startswith("halted")
    assert rows(conn, "SELECT * FROM orders") == []


def test_not_enough_free_cash_skips_the_buy(cfg, conn, clock, tmp_path):
    cfg = replace(cfg, dry_run_starting_eur=D(20))
    bot = make_bot(cfg, conn, clock, tmp_path)
    assert "not enough free EUR" in run_cycle(bot)
    assert rows(conn, "SELECT * FROM orders") == []


# ---- reconcile.check ------------------------------------------------------------------

def check(expected, actual):
    return reconcile.check(expected, actual, ["EUR", "ETH"], tolerance_pct=D("0.005"),
                           abs_eur=D("0.02"), abs_crypto=D("0.000001"))


def test_reconcile_tolerances():
    assert check({"EUR": D(400), "ETH": D("0.0728")}, {"EUR": D("400.01"), "ETH": D("0.0728")}) == []
    assert check({"EUR": D(400)}, {"EUR": D("401.9")}) == []                  # within 0.5%
    bad = check({"EUR": D(400)}, {"EUR": D(800)})
    assert [m.asset for m in bad] == ["EUR"] and bad[0].diff == D(400)
    assert [m.asset for m in check({}, {"ETH": D("0.01")})] == ["ETH"]       # coins the ledger never saw
    assert [m.asset for m in check({"ETH": D("0.01")}, {})] == ["ETH"]       # coins that vanished
