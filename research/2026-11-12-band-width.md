# Price band 10% vs 15% (max 2 open lots within the range)

Script: `research/band_width_backtest.py`. Only `band_width_pct` changes; everything else as in `config.toml`.
Pot EUR 400 per setup. "Shared" is the live BTC+ETH config; the others are one pair alone.

Direction: a wider band is **stricter**. With 15%, two open lots inside any 15% range block a third, so fewer buys are allowed.

| Setup | Window | 10% | 15% |
|---|---|---|---|
| BTC+ETH shared | all (from 2026-02-07) | +21.3% (41 buys) | +23.9% (42) |
| BTC+ETH shared | last 182 days | +16.2% (30) | +18.2% (30) |
| BTC+ETH shared | last 90 days | +1.2% (17) | +2.0% (16) |
| BTC alone | all / 182d / 90d | +9.9 / +7.2 / -0.4% | +10.8 / +8.1 / +0.5% |
| ETH alone | all / 182d / 90d | +11.7 / +9.0 / +1.5% | +13.1 / +10.1 / +1.5% |
| SOL alone | all / 182d / 90d | +10.4 / +6.9 / -7.9% | +4.6 / +1.1 / -5.1% |
| XRP alone | all / 182d / 90d | +10.1 / +6.0 / -0.4% | +4.9 / +0.8 / -0.7% |

Reading it: BTC and ETH gain a little in every window (+0.9 to +2.6 points); SOL and XRP lose about 5 points over the long windows.
Lowest equity is the same or better for 15% on BTC, ETH and shared. The gain on BTC/ETH is small and the sample is one mostly falling market,
so this is not yet enough evidence to change the default (CLAUDE.md: re-open decisions only with new evidence). Worth a second look with more history.

## Added: 20%

| Setup | Window | 10% | 15% | 20% |
|---|---|---|---|---|
| BTC+ETH shared | all | +21.3% | +23.9% | +18.7% |
| BTC+ETH shared | 182d | +16.2% | +18.2% | +11.8% |
| BTC+ETH shared | 90d | +1.2% | +2.0% | +2.0% |
| BTC alone | all / 182d | +9.9 / +7.2% | +10.8 / +8.1% | +8.4 / +5.7% |
| ETH alone | all / 182d | +11.7 / +9.0% | +13.1 / +10.1% | +10.4 / +6.1% |
| SOL alone | all / 182d | +10.4 / +6.9% | +4.6 / +1.1% | +2.7 / -0.8% |
| XRP alone | all / 182d | +10.1 / +6.0% | +4.9 / +0.8% | +4.9 / +0.8% |

20% is worse than 15% on BTC, ETH and shared in the long windows, and worse than 10% on all of them. It does lower the worst dip
(shared lowest equity 343 -> 353 -> 366) because fewer lots are open, but costs profit. For BTC+ETH, 15% looks like the peak, 10% is close behind.
