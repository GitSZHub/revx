"""Order engine: the only code that creates, sends, tracks and cancels orders.

Guarantees:
- Floor guard. Every sell passes through _submit(), which refuses any price below the lot's
  floor. Sells are limit orders, so a fill cannot be below the order price.
- Idempotency. The order row (with its client_order_id) is committed before anything is sent.
  If the outcome of a send is unknown, the row stays 'pending_send' and recover_pending()
  resolves it by looking the client_order_id up on the exchange. It is never blindly re-sent.
- Dry-run. A DryRunBroker simulates fills from live tickers; rows carry dry_run = 1. An Engine
  built for dry-run cannot hold a live broker, and the other way round.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from revx import ledger, risk
from revx.client import RevxApiError, RevxClient, RevxNetworkError
from revx.ledger import dec, txt
from revx.models import Fees, PairSpec, Ticker, split_symbol, to_decimal

ZERO = Decimal(0)
ACTIVE = ("open", "partially_filled")
TERMINAL = ("filled", "cancelled", "rejected", "failed")
DAY_MS = 86_400_000


class FloorViolation(Exception):
    """A sell below a lot's floor was attempted. This is a bug in the caller; nothing was sent."""


class OrderBudgetExceeded(Exception):
    pass


@dataclass
class OrderStatus:
    state: str                       # open, partially_filled, filled, cancelled, rejected
    filled_qty: Decimal = ZERO
    avg_price: Decimal | None = None
    fee: Decimal = ZERO
    fee_currency: str | None = None
    venue_order_id: str | None = None
    reason: str | None = None


# ---- brokers --------------------------------------------------------------------

_STATE_MAP = {
    "pending_new": "open", "new": "open", "partially_filled": "partially_filled",
    "filled": "filled", "cancelled": "cancelled", "rejected": "rejected", "replaced": "cancelled",
}


class LiveBroker:
    """Talks to Revolut X. The order and fill field names follow the OpenAPI spec; they have not
    yet been seen in a real response because the account has never placed an order via the API."""
    is_live = True

    def __init__(self, client: RevxClient):
        self._client = client

    def submit(self, o: sqlite3.Row) -> OrderStatus:
        body = {
            "client_order_id": o["client_order_id"],
            "symbol": o["symbol"],
            "side": o["side"],
            "order_configuration": {"limit": {
                "base_size": o["qty"],
                "price": o["price"],
                # Buys rest on the book (maker). Sells may cross: the limit price is the protection.
                "execution_instructions": ["post_only"] if o["side"] == "buy" else ["allow_taker"],
            }},
        }
        data = self._client._post_order(body)["data"]
        return OrderStatus(_STATE_MAP.get(data["state"], "open"), venue_order_id=data["venue_order_id"])

    def poll(self, o: sqlite3.Row) -> OrderStatus:
        return self._status(self._client.get_order(o["venue_order_id"])["data"])

    def cancel(self, o: sqlite3.Row) -> None:
        self._client._cancel_order(o["venue_order_id"])

    def find_by_client_id(self, o: sqlite3.Row, now_ms: int) -> OrderStatus | None:
        candidates = self._client.active_orders([o["symbol"]])
        candidates += self._client.historical_orders(o["created_at"] - 3_600_000, now_ms + 60_000, [o["symbol"]])
        for item in candidates:
            if item.get("client_order_id") == o["client_order_id"]:
                return self._status(self._client.get_order(item["id"])["data"])
        return None

    @staticmethod
    def _status(d: dict) -> OrderStatus:
        avg = d.get("average_fill_price")
        return OrderStatus(
            state=_STATE_MAP.get(d.get("status") or d.get("state"), "open"),
            filled_qty=to_decimal(d.get("filled_quantity") or "0"),
            avg_price=to_decimal(avg) if avg not in (None, "") else None,
            fee=to_decimal(d.get("total_fee") or "0"),
            fee_currency=d.get("fee_currency"),
            venue_order_id=d.get("id"),
            reason=d.get("reject_reason"),
        )


class DryRunBroker:
    """Simulates the exchange from live tickers. Nothing is sent anywhere.

    A resting buy at price p fills in full once the ask is at or below p. A sell fills once the
    bid is at or above its price. Buys pay the maker fee, sells the taker fee.
    """
    is_live = False

    def __init__(self) -> None:
        self.tickers: dict[str, Ticker] = {}
        self.fees = Fees(ZERO, ZERO)
        self._cancelled: set[str] = set()

    def submit(self, o: sqlite3.Row) -> OrderStatus:
        venue_id = "dry-" + uuid.uuid4().hex
        ticker = self.tickers.get(o["symbol"])
        if o["side"] == "buy" and ticker and dec(o["price"]) >= ticker.ask:
            return OrderStatus("rejected", venue_order_id=venue_id, reason="post-only order would cross")
        return OrderStatus("open", venue_order_id=venue_id)

    def poll(self, o: sqlite3.Row) -> OrderStatus:
        venue_id = o["venue_order_id"]
        if venue_id in self._cancelled:
            return OrderStatus("cancelled", venue_order_id=venue_id)
        ticker = self.tickers.get(o["symbol"])
        price, qty = dec(o["price"]), dec(o["qty"])
        _, quote = split_symbol(o["symbol"])
        if ticker:
            if o["side"] == "buy" and ticker.ask <= price:
                return OrderStatus("filled", qty, price, qty * price * self.fees.maker, quote, venue_id)
            if o["side"] == "sell" and ticker.bid >= price:
                return OrderStatus("filled", qty, price, qty * price * self.fees.taker, quote, venue_id)
        return OrderStatus("open", venue_order_id=venue_id)

    def cancel(self, o: sqlite3.Row) -> None:
        self._cancelled.add(o["venue_order_id"])

    def find_by_client_id(self, o: sqlite3.Row, now_ms: int) -> OrderStatus | None:
        return None


# ---- engine ---------------------------------------------------------------------

class Engine:
    def __init__(self, conn: sqlite3.Connection, broker, *, dry_run: bool, min_profit: Decimal,
                 daily_order_budget: int, interval_minutes: int,
                 clock: Callable[[], int] = ledger.now_ms):
        if broker.is_live == dry_run:
            raise ValueError("broker and mode disagree: dry-run needs DryRunBroker, live needs LiveBroker")
        self.conn = conn
        self.broker = broker
        self.dry_run = dry_run
        self.min_profit = min_profit
        self.daily_order_budget = daily_order_budget
        self.interval_ms = interval_minutes * 60_000
        self.clock = clock
        self.pairs: dict[str, PairSpec] = {}     # refreshed by the cycle
        self.fees = Fees(ZERO, ZERO)             # refreshed by the cycle

    # ---- placing ---------------------------------------------------------

    def place_buy(self, symbol: str, price: Decimal, qty: Decimal, reason: str = "") -> int:
        if price <= 0 or qty <= 0:
            raise ValueError("buy needs a positive price and quantity")
        return self._create_and_submit(symbol, "buy", "entry", price, qty, None, reason)

    def place_sell(self, lot_id: int, price: Decimal, qty: Decimal | None = None,
                   purpose: str = "exit", reason: str = "") -> int:
        """The only way to create a sell. Always tied to a lot; never below that lot's floor."""
        lot = ledger.get_lot(self.conn, lot_id)
        if lot is None or lot.status != "open":
            raise ValueError(f"lot {lot_id} is not open")
        if lot.dry_run != self.dry_run:
            raise ValueError(f"lot {lot_id} belongs to the other mode (dry_run={lot.dry_run})")
        self._check_floor(lot, price)
        available = self.sellable(lot_id)
        qty = available if qty is None else qty
        if qty <= 0 or qty > available:
            raise ValueError(f"lot {lot_id}: cannot sell {qty}, only {available} is free")
        return self._create_and_submit(lot.symbol, "sell", purpose, price, qty, lot_id, reason)

    def sellable(self, lot_id: int) -> Decimal:
        """Lot quantity not already committed to a working sell order."""
        lot = ledger.get_lot(self.conn, lot_id)
        if lot is None or lot.status != "open":
            return ZERO
        reserved = sum(
            (dec(o["qty"]) - dec(o["filled_qty"])
             for o in ledger.active_orders(self.conn, self.dry_run, side="sell", lot_id=lot_id)),
            ZERO,
        )
        return lot.qty_open - reserved

    def orders_sent_last_day(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM orders WHERE dry_run = ? AND created_at > ?",
            (int(self.dry_run), self.clock() - DAY_MS),
        ).fetchone()[0]

    def _check_floor(self, lot, price: Decimal) -> None:
        if price < lot.floor:
            message = f"refused sell of lot {lot.id} at {price}: below floor {lot.floor}"
            ledger.add_event(self.conn, "error", "floor_guard", message, self.clock())
            raise FloorViolation(message)

    def _create_and_submit(self, symbol: str, side: str, purpose: str, price: Decimal, qty: Decimal,
                           lot_id: int | None, reason: str) -> int:
        if self.orders_sent_last_day() >= self.daily_order_budget:
            raise OrderBudgetExceeded(f"daily order budget of {self.daily_order_budget} is used up")
        now = self.clock()
        with self.conn:   # committed before anything is sent
            cur = self.conn.execute(
                "INSERT INTO orders (client_order_id, lot_id, symbol, side, purpose, price, qty, state, "
                "reason, created_at, updated_at, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_send', ?, ?, ?, ?)",
                (str(uuid.uuid4()), lot_id, symbol, side, purpose, txt(price), txt(qty), reason, now, now,
                 int(self.dry_run)),
            )
        order_id = cur.lastrowid
        self._submit(order_id)
        return order_id

    def _submit(self, order_id: int) -> None:
        """Send one 'pending_send' order. Every order, including recovered ones, goes through here."""
        o = self._order(order_id)
        if o["state"] != "pending_send":
            raise ValueError(f"order {order_id} is {o['state']}, not pending_send")
        if o["dry_run"] != int(self.dry_run):
            raise ValueError(f"order {order_id} belongs to the other mode")
        if o["side"] == "sell":
            lot = ledger.get_lot(self.conn, o["lot_id"]) if o["lot_id"] else None
            if lot is None or lot.status != "open" or dec(o["price"]) < lot.floor:
                self._set_state(order_id, "failed", "floor guard: sell is below the lot's floor or has no open lot")
                message = f"refused to send sell order {order_id} at {o['price']}"
                ledger.add_event(self.conn, "error", "floor_guard", message, self.clock())
                raise FloorViolation(message)
        try:
            status = self.broker.submit(o)
        except RevxNetworkError as exc:
            # Unknown outcome: the order may be on the exchange. Leave it for recover_pending().
            ledger.add_event(self.conn, "warning", "order_unknown",
                             f"order {order_id} send outcome unknown: {exc}", self.clock())
            return
        except RevxApiError as exc:
            if exc.status >= 500:
                ledger.add_event(self.conn, "warning", "order_unknown",
                                 f"order {order_id} send outcome unknown: {exc}", self.clock())
                return
            self._set_state(order_id, "rejected", f"exchange refused: {exc.message}")
            return
        with self.conn:
            self.conn.execute("UPDATE orders SET sent_at = ? WHERE id = ?", (self.clock(), order_id))
        self._apply(order_id, status)

    # ---- tracking --------------------------------------------------------

    def recover_pending(self, min_age_ms: int = 60_000) -> None:
        """Resolve orders whose send outcome is unknown (crash or timeout between commit and reply)."""
        now = self.clock()
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE state = 'pending_send' AND dry_run = ?", (int(self.dry_run),)
        ).fetchall()
        for o in rows:
            if now - o["created_at"] < min_age_ms:
                continue   # give the exchange time to list it
            found = self.broker.find_by_client_id(o, now)
            if found is None:
                self._set_state(o["id"], "failed", "never reached the exchange")
            else:
                self._apply(o["id"], found)

    def sync_orders(self, cancel_buys_after_minutes: int | None = None) -> None:
        """Pull the status of every working order and book new fills. Then cancel stale buys."""
        for o in self._working():
            self._apply(o["id"], self.broker.poll(o))
        if cancel_buys_after_minutes is None:
            return
        cutoff = self.clock() - cancel_buys_after_minutes * 60_000
        for o in self._working():
            if o["side"] == "buy" and o["created_at"] <= cutoff:
                self.cancel(o["id"], "unfilled, re-evaluating")

    def cancel(self, order_id: int, reason: str = "") -> None:
        o = self._order(order_id)
        if o["state"] not in ACTIVE:
            return
        try:
            self.broker.cancel(o)
        except RevxApiError as exc:
            if exc.status not in (404, 409):   # already gone or already final
                raise
        status = self.broker.poll(o)
        self._apply(order_id, status)
        if reason and status.state == "cancelled":
            with self.conn:
                self.conn.execute("UPDATE orders SET reason = ? WHERE id = ?", (reason, order_id))

    def _working(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE dry_run = ? AND state IN ('open', 'partially_filled') ORDER BY id",
            (int(self.dry_run),),
        ).fetchall()

    def _order(self, order_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise ValueError(f"no order {order_id}")
        return row

    def _set_state(self, order_id: int, state: str, reason: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE orders SET state = ?, reason = ?, updated_at = ? WHERE id = ?",
                              (state, reason, self.clock(), order_id))

    # ---- booking fills ---------------------------------------------------

    def _apply(self, order_id: int, st: OrderStatus) -> None:
        """Book the difference between the ledger's view of an order and the broker's, atomically."""
        o = self._order(order_id)
        old_filled = dec(o["filled_qty"]) or ZERO
        old_avg = dec(o["avg_fill_price"]) or ZERO
        old_fee = dec(o["fee"]) or ZERO
        filled = max(st.filled_qty, old_filled)
        delta = filled - old_filled
        _, quote = split_symbol(o["symbol"])
        now = self.clock()

        with self.conn:
            avg = st.avg_price if st.avg_price is not None else (dec(o["avg_fill_price"]) or dec(o["price"]))
            fee_total = max(st.fee, old_fee)
            fee_currency = st.fee_currency or o["fee_currency"] or quote
            if delta > 0:
                fill_price = (avg * filled - old_avg * old_filled) / delta
                fee_delta = fee_total - old_fee
                self.conn.execute(
                    "INSERT INTO fills (order_id, ts, symbol, side, price, qty, quote_amount, fee, fee_currency, "
                    "dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (order_id, now, o["symbol"], o["side"], txt(fill_price), txt(delta),
                     txt(delta * fill_price), txt(fee_delta), fee_currency, o["dry_run"]),
                )
                if o["side"] == "sell":
                    self._book_disposal(o, delta, fill_price, fee_delta, fee_currency, now)
            self.conn.execute(
                "UPDATE orders SET state = ?, venue_order_id = COALESCE(venue_order_id, ?), filled_qty = ?, "
                "avg_fill_price = ?, fee = ?, fee_currency = ?, reason = COALESCE(?, reason), updated_at = ? "
                "WHERE id = ?",
                (st.state, st.venue_order_id, txt(filled), txt(avg) if filled > 0 else None,
                 txt(fee_total), fee_currency, st.reason, now, order_id),
            )
            if o["side"] == "buy" and st.state in TERMINAL and filled > 0 and o["lot_id"] is None:
                lot_id = self._open_lot(o, filled, avg, fee_total, fee_currency, now)
                self.conn.execute("UPDATE orders SET lot_id = ? WHERE id = ?", (lot_id, order_id))

    def _open_lot(self, o: sqlite3.Row, filled: Decimal, avg: Decimal, fee: Decimal,
                  fee_currency: str, now: int) -> int:
        base, _ = split_symbol(o["symbol"])
        spent = filled * avg
        if fee_currency == base:       # fee taken from the coins received
            qty, cost, fee_rate = filled - fee, spent, (fee / filled if filled else ZERO)
        else:                          # fee charged in EUR on top
            qty, cost, fee_rate = filled, spent + fee, (fee / spent if spent else ZERO)
        spec = self.pairs.get(o["symbol"])
        floor = risk.floor_price(avg, fee_rate, self.fees.taker, self.min_profit,
                                 spec.price_step if spec else None)
        last_closed = (now // self.interval_ms) * self.interval_ms - self.interval_ms
        cur = self.conn.execute(
            "INSERT INTO lots (symbol, source, status, entry_price, qty_initial, qty_open, cost_basis_eur, "
            "buy_fee_rate, sell_fee_rate, floor, last_candle_ms, opened_at, dry_run) "
            "VALUES (?, 'bot', 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (o["symbol"], txt(avg), txt(qty), txt(qty), txt(cost), txt(fee_rate), txt(self.fees.taker),
             txt(floor), last_closed, now, o["dry_run"]),
        )
        return cur.lastrowid

    def _book_disposal(self, o: sqlite3.Row, qty: Decimal, price: Decimal, fee: Decimal,
                       fee_currency: str, now: int) -> None:
        """Record a sale of (part of) a lot: EUR proceeds, fee, cost basis and realized P&L."""
        lot = ledger.get_lot(self.conn, o["lot_id"])
        base, _ = split_symbol(o["symbol"])
        proceeds = qty * price
        fee_eur = fee * price if fee_currency == base else fee
        cost = lot.cost_basis_eur * qty / lot.qty_initial
        pnl = proceeds - fee_eur - cost
        self.conn.execute(
            "INSERT INTO disposals (lot_id, order_id, ts, qty, price, proceeds_eur, fee_eur, cost_basis_eur, "
            "realized_pnl_eur, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lot.id, o["id"], now, txt(qty), txt(price), txt(proceeds), txt(fee_eur), txt(cost), txt(pnl),
             o["dry_run"]),
        )
        remaining = lot.qty_open - qty
        spec = self.pairs.get(o["symbol"])
        dust = spec.min_base if spec else ZERO
        totals = self.conn.execute(
            "SELECT qty, proceeds_eur, realized_pnl_eur FROM disposals WHERE lot_id = ?", (lot.id,)
        ).fetchall()
        sold = sum((dec(r["qty"]) for r in totals), ZERO)
        avg_exit = sum((dec(r["proceeds_eur"]) for r in totals), ZERO) / sold
        realized = sum((dec(r["realized_pnl_eur"]) for r in totals), ZERO)
        closed = remaining <= 0 or remaining < dust
        self.conn.execute(
            "UPDATE lots SET qty_open = ?, exit_price = ?, realized_pnl_eur = ?, status = ?, closed_at = ? "
            "WHERE id = ?",
            (txt(max(remaining, ZERO)), txt(avg_exit), txt(realized),
             "closed" if closed else "open", now if closed else None, lot.id),
        )
