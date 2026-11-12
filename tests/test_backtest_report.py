from dataclasses import replace
from decimal import Decimal as D

from conftest import FEES, IV_MS, make_engine, synthetic_candles, ticker
from revx import backtest, ledger
from revx.models import Candle
from revx.report import build_report
from revx.signals import make_signal


def series_for(cfg, raw):
    return backtest.build_series("ETH-EUR", [Candle.from_api(c) for c in raw], cfg)


def extend(raw, closes):
    raw = list(raw)
    for close in closes:
        prev = raw[-1]
        raw.append({"start": prev["start"] + IV_MS, "open": prev["close"],
                    "high": str(max(D(prev["close"]), D(close)) + 4),
                    "low": str(min(D(prev["close"]), D(close)) - 4), "close": str(close), "volume": "10"})
    return raw


def test_backtest_buys_the_dip_and_sells_above_the_floor(cfg):
    raw = extend(synthetic_candles(), [1950, 1990, 2080, 2085, 2050, 2040])
    result = backtest.run_backtest({"ETH-EUR": series_for(cfg, raw)}, cfg, FEES, make_signal(cfg.strategy))
    assert len(result.trades) >= 1
    for t in result.trades:
        assert t.exit_price >= t.floor
        assert t.pnl > 0
    assert result.equity > result.start_cash
    text = backtest.format_result(result)
    assert "Backtest" in text and "buys" in text


def test_backtest_never_sells_below_floor_and_rests_at_it(cfg):
    # The slide's first close below the band is 1961.93 (floor 2022.56). It arms at 2040, then
    # collapses: the close is below the floor, so the sale must wait at the floor.
    raw = extend(synthetic_candles(), [2000, 2040, 1900, 1850])
    result = backtest.run_backtest({"ETH-EUR": series_for(cfg, raw)}, cfg, FEES, make_signal(cfg.strategy))
    dip_lot = next(l for l in result.open_lots if l.entry == D("1961.93"))
    assert dip_lot.resting
    assert all(t.entry != D("1961.93") for t in result.trades)

    raw = extend(raw, [1950, 2030])           # the high reaches the floor again
    result = backtest.run_backtest({"ETH-EUR": series_for(cfg, raw)}, cfg, FEES, make_signal(cfg.strategy))
    sold = next(t for t in result.trades if t.entry == D("1961.93"))
    assert sold.exit_price == sold.floor


def test_backtest_respects_the_lot_cap_and_cash(cfg):
    cfg = replace(cfg, dry_run_starting_eur=D(60))
    raw = extend(synthetic_candles(), [1850, 1790, 1730, 1670])
    result = backtest.run_backtest({"ETH-EUR": series_for(cfg, raw)}, cfg, FEES, make_signal(cfg.strategy))
    assert len(result.open_lots) == 1 and result.cash < D(60)


def test_report_shows_lots_pnl_and_unmanaged_coins(cfg, conn, clock, tmp_path):
    engine = make_engine(conn, clock)
    ledger.add_cash_flow(conn, "opening_balance", "EUR", D(400), dry_run=True)
    engine.broker.tickers = {"ETH-EUR": ticker("ETH-EUR", 1999, 2001)}
    engine.place_buy("ETH-EUR", D("1999"), D("0.025"))
    engine.broker.tickers = {"ETH-EUR": ticker("ETH-EUR", 1997, 1999)}
    engine.sync_orders()

    prices = {"ETH-EUR": ticker("ETH-EUR", 2100), "BTC-EUR": ticker("BTC-EUR", 60000)}
    text = build_report(conn, dry_run=True, tickers=prices, pairs=cfg.strategy.pairs,
                        target_multiple=cfg.target_multiple, halt_file=tmp_path / "HALT")
    assert "DRY-RUN" in text and "Open lots: 1" in text
    assert "Money put in:   EUR 400.00" in text
    assert "Equity now:     EUR 402.52" in text          # 350.025 cash + 0.025 x 2100
    assert "waiting for floor" in text and "1.01x of the 10x target" in text

    ledger.add_cash_flow(conn, "opening_balance", "ETH", D("0.0728"), eur_value=D("140"))
    live = build_report(conn, dry_run=False, tickers=prices, pairs=cfg.strategy.pairs,
                        target_multiple=cfg.target_multiple, halt_file=tmp_path / "HALT")
    assert "LIVE" in live and "Open lots: 0" in live
    assert "not managed by the bot" in live and "0.0728 ETH" in live
