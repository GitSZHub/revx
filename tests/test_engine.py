from decimal import Decimal as D

import pytest

from conftest import make_engine, ticker
from revx import ledger
from revx.client import RevxApiError, RevxNetworkError
from revx.engine import DryRunBroker, Engine, FloorViolation, OrderBudgetExceeded, OrderStatus

SYM = "ETH-EUR"


def order(conn, order_id):
    return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.fixture
def engine(conn, clock):
    e = make_engine(conn, clock)
    e.broker.tickers = {SYM: ticker(SYM, 1999, 2001)}
    ledger.add_cash_flow(conn, "opening_balance", "EUR", D(400), dry_run=True)
    return e


def filled_lot(engine, conn, price="1999", qty="0.025"):
    oid = engine.place_buy(SYM, D(price), D(qty))
    engine.broker.tickers = {SYM: ticker(SYM, D(price) - 2, price)}
    engine.sync_orders()
    return ledger.get_lot(conn, order(conn, oid)["lot_id"])


# ---- dry-run lifecycle ------------------------------------------------------------

def test_dry_run_buy_rests_then_fills_and_opens_a_lot(engine, conn):
    oid = engine.place_buy(SYM, D("1999"), D("0.025"))
    assert order(conn, oid)["state"] == "open" and order(conn, oid)["dry_run"] == 1
    engine.sync_orders()
    assert order(conn, oid)["state"] == "open" and count(conn, "lots") == 0

    engine.broker.tickers = {SYM: ticker(SYM, 1997, 1999)}     # the ask comes down to our bid
    engine.sync_orders()
    assert order(conn, oid)["state"] == "filled"
    lot = ledger.get_lot(conn, order(conn, oid)["lot_id"])
    assert lot.dry_run and lot.entry_price == D("1999") and lot.qty_open == D("0.025")
    assert lot.cost_basis_eur == D("49.975")
    assert lot.floor == D("2060.77")                           # 1999 x 1.0309, rounded up to 0.01
    fill = conn.execute("SELECT * FROM fills").fetchone()
    assert fill["dry_run"] == 1 and ledger.dec(fill["fee"]) == 0


def test_post_only_buy_that_would_cross_is_rejected(engine, conn):
    oid = engine.place_buy(SYM, D("2001"), D("0.025"))
    assert order(conn, oid)["state"] == "rejected" and count(conn, "lots") == 0


def test_sell_books_a_disposal_with_cost_basis_and_pnl(engine, conn):
    lot = filled_lot(engine, conn)
    oid = engine.place_sell(lot.id, D("2100"))
    engine.broker.tickers = {SYM: ticker(SYM, 2100, 2101)}
    engine.sync_orders()
    assert order(conn, oid)["state"] == "filled"

    d = conn.execute("SELECT * FROM disposals").fetchone()
    assert ledger.dec(d["proceeds_eur"]) == D("52.5")
    assert ledger.dec(d["fee_eur"]) == D("0.04725")
    assert ledger.dec(d["cost_basis_eur"]) == D("49.975")
    assert ledger.dec(d["realized_pnl_eur"]) == D("2.47775")
    closed = ledger.get_lot(conn, lot.id)
    assert closed.status == "closed" and closed.qty_open == 0
    assert ledger.holdings(conn, dry_run=True)["EUR"] == D(400) - D("49.975") + D("52.5") - D("0.04725")
    assert ledger.holdings(conn, dry_run=True)["ETH"] == 0
    assert ledger.holdings(conn, dry_run=False) == {}          # nothing leaks into live accounting


def test_partial_sell_splits_cost_basis(engine, conn):
    lot = filled_lot(engine, conn)
    engine.place_sell(lot.id, D("2100"), D("0.01"), "partial_exit")
    engine.broker.tickers = {SYM: ticker(SYM, 2100, 2101)}
    engine.sync_orders()
    after = ledger.get_lot(conn, lot.id)
    assert after.status == "open" and after.qty_open == D("0.015")
    d = conn.execute("SELECT * FROM disposals").fetchone()
    assert ledger.dec(d["cost_basis_eur"]) == D("19.99")
    assert engine.sellable(lot.id) == D("0.015")


# ---- the floor guard --------------------------------------------------------------

def test_place_sell_below_floor_is_refused_and_nothing_is_written(engine, conn):
    lot = filled_lot(engine, conn)
    before = count(conn, "orders")
    with pytest.raises(FloorViolation):
        engine.place_sell(lot.id, lot.floor - D("0.01"))
    assert count(conn, "orders") == before
    assert conn.execute("SELECT kind FROM events ORDER BY id DESC").fetchone()["kind"] == "floor_guard"


def test_sell_exactly_at_floor_is_allowed(engine, conn):
    lot = filled_lot(engine, conn)
    assert order(conn, engine.place_sell(lot.id, lot.floor))["state"] == "open"


def test_submit_refuses_a_below_floor_sell_even_if_the_row_already_exists(engine, conn):
    """Defence in depth: a sell row that got into the ledger some other way still cannot be sent."""
    lot = filled_lot(engine, conn)
    sent = []
    real_submit = engine.broker.submit
    engine.broker.submit = lambda o: sent.append(o) or real_submit(o)
    with conn:
        cur = conn.execute(
            "INSERT INTO orders (client_order_id, lot_id, symbol, side, purpose, price, qty, state, created_at, "
            "updated_at, dry_run) VALUES ('rogue', ?, ?, 'sell', 'exit', '1500', '0.025', 'pending_send', 0, 0, 1)",
            (lot.id, SYM))
    with pytest.raises(FloorViolation):
        engine._submit(cur.lastrowid)
    assert sent == []
    assert order(conn, cur.lastrowid)["state"] == "failed"


def test_every_sell_needs_an_open_lot_of_the_same_mode(engine, conn):
    with pytest.raises(ValueError):
        engine.place_sell(999, D("5000"))
    lot = filled_lot(engine, conn)
    with pytest.raises(ValueError):
        engine.place_sell(lot.id, D("2100"), D("1"))           # more than the lot holds
    engine.place_sell(lot.id, D("2100"))
    with pytest.raises(ValueError):
        engine.place_sell(lot.id, D("2100"))                   # already fully committed


# ---- idempotent placement -----------------------------------------------------------

class FakeLiveBroker:
    is_live = True

    def __init__(self, conn):
        self.conn = conn
        self.submits = []
        self.submit_error = None
        self.found = None
        self.polls = []
        self.cancels = []

    def submit(self, o):
        # The order row must already be committed when anything is sent.
        row = self.conn.execute("SELECT state FROM orders WHERE client_order_id = ?",
                                (o["client_order_id"],)).fetchone()
        assert row is not None and row["state"] == "pending_send"
        self.submits.append(o["client_order_id"])
        if self.submit_error:
            raise self.submit_error
        return OrderStatus("open", venue_order_id="v-" + o["client_order_id"][:8])

    def find_by_client_id(self, o, now_ms):
        return self.found

    def poll(self, o):
        return self.polls.pop(0)

    def cancel(self, o):
        self.cancels.append(o["venue_order_id"])


@pytest.fixture
def live(conn, clock):
    broker = FakeLiveBroker(conn)
    return make_engine(conn, clock, broker=broker, dry_run=False), broker


def test_order_row_is_committed_before_sending_and_uses_a_uuid(live, conn):
    engine, broker = live
    oid = engine.place_buy(SYM, D("1999"), D("0.025"))
    row = order(conn, oid)
    assert row["state"] == "open" and row["dry_run"] == 0
    assert broker.submits == [row["client_order_id"]]
    assert len(row["client_order_id"]) == 36 and row["venue_order_id"].startswith("v-")


def test_timeout_leaves_the_order_pending_and_is_not_resent(live, conn, clock):
    engine, broker = live
    broker.submit_error = RevxNetworkError("timeout")
    oid = engine.place_buy(SYM, D("1999"), D("0.025"))
    assert order(conn, oid)["state"] == "pending_send"
    assert len(broker.submits) == 1

    engine.recover_pending()                 # too soon: left alone
    assert order(conn, oid)["state"] == "pending_send"

    clock.now += 120_000
    broker.found = OrderStatus("open", venue_order_id="v-found")   # it did reach the exchange
    engine.recover_pending()
    row = order(conn, oid)
    assert row["state"] == "open" and row["venue_order_id"] == "v-found"
    assert len(broker.submits) == 1          # adopted, never sent twice


def test_pending_order_not_on_the_exchange_is_failed_not_resent(live, conn, clock):
    engine, broker = live
    broker.submit_error = RevxNetworkError("timeout")
    oid = engine.place_buy(SYM, D("1999"), D("0.025"))
    clock.now += 120_000
    engine.recover_pending()
    assert order(conn, oid)["state"] == "failed"
    assert len(broker.submits) == 1


def test_recovered_order_that_already_filled_opens_exactly_one_lot(live, conn, clock):
    engine, broker = live
    broker.submit_error = RevxApiError(503, "unavailable")
    oid = engine.place_buy(SYM, D("1999"), D("0.025"))
    assert order(conn, oid)["state"] == "pending_send"
    clock.now += 120_000
    broker.found = OrderStatus("filled", D("0.025"), D("1998.5"), D(0), "EUR", "v-1")
    engine.recover_pending()
    engine.recover_pending()
    assert count(conn, "lots") == 1 and count(conn, "fills") == 1
    assert ledger.get_lot(conn, order(conn, oid)["lot_id"]).entry_price == D("1998.5")


def test_exchange_refusal_marks_the_order_rejected(live, conn):
    engine, broker = live
    broker.submit_error = RevxApiError(400, "No such pair")
    oid = engine.place_buy(SYM, D("1999"), D("0.025"))
    assert order(conn, oid)["state"] == "rejected"


def test_partial_fill_then_cancel_opens_a_lot_for_the_filled_part(live, conn, clock):
    engine, broker = live
    oid = engine.place_buy(SYM, D("1999"), D("0.025"))
    broker.polls = [OrderStatus("partially_filled", D("0.01"), D("1999"), D(0), "EUR"),
                    OrderStatus("cancelled", D("0.01"), D("1999"), D(0), "EUR")]
    clock.now += 31 * 60_000
    engine.sync_orders(cancel_buys_after_minutes=30)
    assert broker.cancels and order(conn, oid)["state"] == "cancelled"
    lot = ledger.get_lot(conn, order(conn, oid)["lot_id"])
    assert lot.qty_open == D("0.01") and not lot.dry_run
    assert count(conn, "fills") == 1


def test_fee_taken_in_coins_reduces_the_lot_quantity(live, conn):
    engine, broker = live
    oid = engine.place_buy(SYM, D("2000"), D("0.025"))
    broker.polls = [OrderStatus("filled", D("0.025"), D("2000"), D("0.000025"), "ETH")]
    engine.sync_orders()
    lot = ledger.get_lot(conn, order(conn, oid)["lot_id"])
    assert lot.qty_open == D("0.024975") and lot.cost_basis_eur == D("50")
    assert ledger.holdings(conn, dry_run=False)["ETH"] == D("0.024975")


# ---- mode separation and budget ------------------------------------------------------

def test_engine_refuses_a_broker_of_the_other_mode(conn, clock):
    with pytest.raises(ValueError):
        Engine(conn, DryRunBroker(), dry_run=False, min_profit=D("0.03"), daily_order_budget=10,
               interval_minutes=240, clock=clock)
    with pytest.raises(ValueError):
        Engine(conn, FakeLiveBroker(conn), dry_run=True, min_profit=D("0.03"), daily_order_budget=10,
               interval_minutes=240, clock=clock)


def test_live_engine_cannot_sell_a_dry_run_lot(engine, conn, clock):
    lot = filled_lot(engine, conn)
    live_engine = make_engine(conn, clock, broker=FakeLiveBroker(conn), dry_run=False)
    with pytest.raises(ValueError):
        live_engine.place_sell(lot.id, D("2100"))


def test_daily_order_budget(conn, clock):
    engine = make_engine(conn, clock, budget=2)
    engine.broker.tickers = {SYM: ticker(SYM, 1999, 2001)}
    engine.place_buy(SYM, D("1999"), D("0.01"))
    engine.place_buy(SYM, D("1998"), D("0.01"))
    with pytest.raises(OrderBudgetExceeded):
        engine.place_buy(SYM, D("1997"), D("0.01"))
    clock.now += 25 * 3_600_000
    engine.place_buy(SYM, D("1997"), D("0.01"))
