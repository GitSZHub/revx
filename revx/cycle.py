"""One decision cycle, and the Bot object that holds what a cycle needs.

Order of work in a cycle:
  1. stop if halted
  2. refresh pair specs, fees, tickers
  3. resolve unknown orders, sync working orders, cancel stale buys
  4. reconcile the ledger against /balances (live: halt on mismatch; dry-run: warn)
  5. per pair: advance trailing stops, manage exits, decide on an entry, log a signal row
  6. hourly equity snapshot, nightly backup
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from revx import killswitch, ledger, market, reconcile, risk, strategy
from revx.client import RevxError
from revx.config import Config
from revx.engine import Engine, FloorViolation, OrderBudgetExceeded
from revx.ledger import dec
from revx.models import (Candle, Ticker, closed_candles, parse_balances, parse_fees, parse_pairs,
                         parse_tickers, split_symbol)
from revx.signals import EntrySignal

log = logging.getLogger("revx")
ZERO = Decimal(0)
HOUR_MS = 3_600_000
PAIRS_REFRESH_MS = 24 * HOUR_MS


@dataclass
class Bot:
    cfg: Config
    conn: sqlite3.Connection
    client: Any
    engine: Engine
    signal: EntrySignal
    dry_run: bool
    clock: Callable[[], int] = ledger.now_ms
    pairs: dict = field(default_factory=dict)
    pairs_fetched_at: int = 0
    series_cache: dict = field(default_factory=dict)   # symbol -> (last closed candle start, series)


def run_cycle(bot: Bot) -> str:
    """Run one cycle. Never raises for API trouble: errors are counted and can trip the halt."""
    conn, cfg = bot.conn, bot.cfg
    reason = killswitch.halt_reason(conn, cfg.paths.halt_file)
    if reason:
        killswitch.note_halted(conn, reason)
        return f"halted: {reason}"
    try:
        status = _cycle(bot)
    except FloorViolation as exc:
        killswitch.halt(conn, f"floor guard tripped: {exc}")
        return f"halted: {exc}"
    except RevxError as exc:
        tripped = killswitch.record_error(conn, "api_error", str(exc), cfg.api.max_consecutive_errors)
        return f"api error{' (halted)' if tripped else ''}: {exc}"
    except Exception as exc:   # a bug must not loop forever placing nothing or, worse, something
        log.exception("cycle crashed")
        tripped = killswitch.record_error(conn, "crash", f"{type(exc).__name__}: {exc}",
                                          cfg.api.max_consecutive_errors)
        return f"crash{' (halted)' if tripped else ''}: {type(exc).__name__}: {exc}"
    killswitch.record_success(conn)
    return status


def _cycle(bot: Bot) -> str:
    cfg, conn, client, engine = bot.cfg, bot.conn, bot.client, bot.engine
    now = bot.clock()
    symbols = list(cfg.strategy.pairs)

    if not bot.pairs or now - bot.pairs_fetched_at > PAIRS_REFRESH_MS:
        bot.pairs = parse_pairs(client.pairs())
        bot.pairs_fetched_at = now
    missing = [s for s in symbols if s not in bot.pairs]
    if missing:
        raise RuntimeError(f"pairs not tradable on the exchange: {missing}")
    fees = parse_fees(client.fee_tiers())
    tickers = parse_tickers(client.tickers(symbols))
    engine.pairs, engine.fees = bot.pairs, fees
    if bot.dry_run:
        engine.broker.tickers, engine.broker.fees = tickers, fees

    engine.recover_pending()
    engine.sync_orders(cfg.loop.unfilled_cancel_minutes)

    balances = parse_balances(client.balances())
    _ensure_opening_balances(bot, balances, tickers, now)
    mismatches = _reconcile(bot, balances)
    if mismatches:
        # A fill can land between the order sync and the balance read. Look once more.
        engine.sync_orders()
        mismatches = _reconcile(bot, parse_balances(client.balances()))
    if mismatches:
        text = "reconciliation mismatch: " + "; ".join(str(m) for m in mismatches)
        if not bot.dry_run:
            killswitch.halt(conn, text + ". Record deposits or withdrawals with "
                                         "`main.py deposit` / `main.py withdraw`, then `main.py resume`.")
            return "halted: " + text
        if ledger.get_value(conn, "reconcile_warning") != text:
            ledger.set_value(conn, "reconcile_warning", text)
            ledger.add_event(conn, "warning", "reconcile", text + " (dry-run: not halting)", now)
    elif ledger.get_value(conn, "reconcile_warning"):
        ledger.set_value(conn, "reconcile_warning", "")

    actions = []
    for symbol in symbols:
        actions.append(f"{symbol} {_process_pair(bot, symbol, tickers[symbol], now)}")

    _snapshot(bot, tickers, now)
    _nightly_backup(bot, now)
    return "; ".join(actions)


# ---- balances -------------------------------------------------------------------

def _tracked_assets(bot: Bot) -> list[str]:
    return ["EUR"] + [split_symbol(s)[0] for s in bot.cfg.strategy.pairs]


def _ensure_opening_balances(bot: Bot, balances: dict[str, Decimal], tickers: dict[str, Ticker],
                             now: int) -> None:
    """First run only: take what the account holds as the starting point. Coins recorded here
    belong to no lot, so the bot never sells them."""
    conn = bot.conn
    if conn.execute("SELECT 1 FROM cash_flows WHERE dry_run = 0 LIMIT 1").fetchone() is None:
        for asset in _tracked_assets(bot):
            amount = balances.get(asset, ZERO)
            if amount <= 0:
                continue
            eur_value = amount
            if asset != "EUR":
                ticker = tickers.get(f"{asset}-EUR")
                eur_value = amount * ticker.bid if ticker else None
            ledger.add_cash_flow(conn, "opening_balance", asset, amount, eur_value=eur_value,
                                 note="balance when the bot first ran; not managed by the bot", ts=now)
        ledger.add_event(conn, "info", "opening_balance", "recorded opening balances from the exchange", now)
    if bot.dry_run and conn.execute("SELECT 1 FROM cash_flows WHERE dry_run = 1 LIMIT 1").fetchone() is None:
        ledger.add_cash_flow(conn, "opening_balance", "EUR", bot.cfg.dry_run_starting_eur,
                             note="simulated starting cash", dry_run=True, ts=now)


def _reconcile(bot: Bot, balances: dict[str, Decimal]) -> list[reconcile.Mismatch]:
    cfg = bot.cfg
    return reconcile.check(
        ledger.holdings(bot.conn, dry_run=False), balances, _tracked_assets(bot),
        tolerance_pct=cfg.reconcile_tolerance_pct, abs_eur=cfg.reconcile_abs_eur,
        abs_crypto=cfg.reconcile_abs_crypto,
    )


def free_eur(bot: Bot) -> Decimal:
    """EUR the bot may still commit: ledger cash minus what working buy orders have reserved."""
    cash = ledger.holdings(bot.conn, bot.dry_run).get("EUR", ZERO)
    reserved = sum(
        ((dec(o["qty"]) - dec(o["filled_qty"])) * dec(o["price"])
         for o in ledger.active_orders(bot.conn, bot.dry_run, side="buy")),
        ZERO,
    )
    return cash - reserved


# ---- per pair -------------------------------------------------------------------

def _series(bot: Bot, symbol: str, now: int) -> tuple[list, int]:
    """Indicator series for a pair, recomputed only when a new candle has closed."""
    s = bot.cfg.strategy
    interval_ms = s.candle_interval_minutes * 60_000
    expected_last = (now // interval_ms) * interval_ms - interval_ms
    cached = bot.series_cache.get(symbol)
    if cached and cached[0] == expected_last:
        return cached[1], expected_last
    need = market.candles_needed(s.vol_window, s.vol_lookback_candles) + 20
    raw = bot.client.candles(symbol, s.candle_interval_minutes, since_ms=now - need * interval_ms)
    candles = closed_candles([Candle.from_api(c) for c in raw["data"]], s.candle_interval_minutes, now)
    series = market.compute_series(
        symbol, candles, bollinger_period=s.bollinger_period, bollinger_k=s.bollinger_stddev,
        atr_period=s.atr_period, vol_window=s.vol_window, vol_lookback=s.vol_lookback_candles,
        breakout_atr_mult=s.breakout_atr_mult,
    )
    if series and series[-1] is not None and series[-1].candle_start_ms == expected_last:
        bot.series_cache[symbol] = (expected_last, series)
    return series, expected_last


def _process_pair(bot: Bot, symbol: str, ticker: Ticker, now: int) -> str:
    cfg, conn, engine = bot.cfg, bot.conn, bot.engine
    s = cfg.strategy
    spec = bot.pairs[symbol]

    def signal(action: str, reason: str, **kw) -> str:
        ledger.add_signal(conn, symbol=symbol, action=action, reason=reason, dry_run=bot.dry_run, ts=now, **kw)
        return f"{action} ({reason})"

    series, expected_last = _series(bot, symbol, now)
    state = series[-1] if series else None
    if state is None:
        return signal("skip", "not enough candle history for the indicators", price=ticker.bid)
    if state.candle_start_ms != expected_last:
        return signal("skip", "the latest closed candle is not available yet", state=state, price=ticker.bid)

    # Exits first: they must run even when no entry is possible.
    for lot in ledger.open_lots(conn, bot.dry_run, symbol):
        if not lot.exit_triggered:
            adv = strategy.advance_lot(lot.trail_state, lot.last_candle_ms, series, cfg.exit.trail_atr_mult)
            if adv.last_candle_ms != lot.last_candle_ms:
                ledger.update_lot_trail(conn, lot.id, adv.trail, adv.last_candle_ms, adv.exit)
            if adv.armed_now:
                signal("arm", f"lot {lot.id} reached its floor {lot.floor}; trail stop {adv.trail.trail_stop}",
                       state=state, lot_id=lot.id)
                if not adv.exit:
                    _partial_sell(bot, lot.id, ticker, spec)
            if adv.exit:
                signal("sell", f"lot {lot.id}: close {adv.exit_state.close} below trail stop "
                               f"{adv.trail.trail_stop}", state=adv.exit_state, lot_id=lot.id)
            lot = ledger.get_lot(conn, lot.id)
        if lot.exit_triggered and lot.status == "open":
            _manage_exit(bot, lot, ticker, spec, now)

    # Entry.
    pair_lots = ledger.open_lots(conn, bot.dry_run, symbol)
    working_buys = ledger.active_orders(conn, bot.dry_run, side="buy")
    pending_here = sum(1 for o in working_buys if o["symbol"] == symbol)
    price = risk.quantize_down(ticker.bid, spec.price_step)
    decision = strategy.decide_entry(
        state=state,
        recent_breakout=strategy.breakout_in_window(series, s.breakout_cooldown_candles),
        signal=bot.signal, price=price,
        pair_entries=[lot.entry_price for lot in pair_lots],
        pair_open=len(pair_lots) + pending_here,
        total_open=len(ledger.open_lots(conn, bot.dry_run)) + len(working_buys),
        has_pending_buy=pending_here > 0, cfg=s,
    )
    if not decision.buy:
        return signal("skip", decision.reason, state=state, price=price, regime=decision.regime)

    qty = risk.tranche_quantity(s.tranche_eur, price, spec.base_step, spec.min_base)
    if qty is None or qty * price < spec.min_quote:
        return signal("skip", "tranche is below the exchange minimum order size", state=state, price=price,
                      regime=decision.regime)
    if free_eur(bot) < qty * price:
        return signal("skip", f"not enough free EUR ({free_eur(bot):.2f}) for a {s.tranche_eur} tranche",
                      state=state, price=price, regime=decision.regime)
    try:
        engine.place_buy(symbol, price, qty, decision.reason)
    except OrderBudgetExceeded as exc:
        return signal("skip", str(exc), state=state, price=price, regime=decision.regime)
    return signal("buy", f"{decision.reason}; limit {price} x {qty}", state=state, price=price,
                  regime=decision.regime)


def _exit_price(lot, ticker: Ticker, spec) -> Decimal:
    """The bid if it is at or above the floor, otherwise the floor. Never below the floor."""
    return max(risk.quantize_down(ticker.bid, spec.price_step), lot.floor)


def _manage_exit(bot: Bot, lot, ticker: Ticker, spec, now: int) -> None:
    """Keep one sell order working for a lot whose exit has triggered."""
    engine = bot.engine
    desired = _exit_price(lot, ticker, spec)
    stale_before = now - bot.cfg.loop.unfilled_cancel_minutes * 60_000
    for o in ledger.active_orders(bot.conn, bot.dry_run, side="sell", lot_id=lot.id):
        if o["state"] == "pending_send":
            return
        if o["purpose"] == "exit" and (dec(o["price"]) == desired or o["created_at"] > stale_before):
            return
        engine.cancel(o["id"], "repricing exit")
    qty = risk.quantize_down(engine.sellable(lot.id), spec.base_step)
    if qty <= 0:
        return
    try:
        engine.place_sell(lot.id, desired, qty, "exit", "trail stop hit")
    except OrderBudgetExceeded as exc:
        ledger.add_event(bot.conn, "warning", "order_budget", f"exit for lot {lot.id} delayed: {exc}", now)


def _partial_sell(bot: Bot, lot_id: int, ticker: Ticker, spec) -> None:
    """Optional: sell exit.partial_pct of a lot when it arms, and trail the rest."""
    pct = bot.cfg.exit.partial_pct
    if pct <= 0:
        return
    lot = ledger.get_lot(bot.conn, lot_id)
    qty = risk.quantize_down(lot.qty_open * pct, spec.base_step)
    price = _exit_price(lot, ticker, spec)
    if qty < spec.min_base or qty * price < spec.min_quote:
        return
    try:
        bot.engine.place_sell(lot_id, price, qty, "partial_exit", "partial sell at arm")
    except OrderBudgetExceeded:
        pass


# ---- housekeeping ---------------------------------------------------------------

def equity(bot: Bot, tickers: dict[str, Ticker]) -> tuple[Decimal, Decimal]:
    """(EUR cash, EUR value of coins) for the bot's mode, coins valued at the bid."""
    held = ledger.holdings(bot.conn, bot.dry_run)
    positions = ZERO
    for symbol in bot.cfg.strategy.pairs:
        base, _ = split_symbol(symbol)
        if symbol in tickers:
            positions += held.get(base, ZERO) * tickers[symbol].bid
    return held.get("EUR", ZERO), positions


def _snapshot(bot: Bot, tickers: dict[str, Ticker], now: int) -> None:
    key = f"last_snapshot_ms_{int(bot.dry_run)}"
    if now - int(ledger.get_value(bot.conn, key, "0")) < HOUR_MS:
        return
    cash, positions = equity(bot, tickers)
    ledger.add_equity_snapshot(bot.conn, cash, positions, bot.dry_run, now)
    ledger.set_value(bot.conn, key, str(now), now)


def _nightly_backup(bot: Bot, now: int) -> None:
    day = datetime.fromtimestamp(now / 1000, timezone.utc).strftime("%Y%m%d")
    if ledger.get_value(bot.conn, "last_backup_day") == day:
        return
    if bot.conn.execute("PRAGMA database_list").fetchone()["file"] == "":
        return   # in-memory ledger (tests)
    target = ledger.backup(bot.conn, bot.cfg.paths.backups)
    ledger.set_value(bot.conn, "last_backup_day", day, now)
    ledger.add_event(bot.conn, "info", "backup", f"ledger backed up to {target.name}", now)
