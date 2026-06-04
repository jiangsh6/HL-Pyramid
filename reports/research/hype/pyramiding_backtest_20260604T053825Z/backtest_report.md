# HYPE Pyramiding Backtest Report

Research-only backtest. Signals are evaluated on candle close and fills happen at the next candle open.

## Summary

This rerun is the bug-fixed v1.1 result. Headline recommendation is
NEEDS_MORE_DATA because the strongest sub-window (W3) is dominated by a
single 96-bar trade still open at end-of-data.

The most actionable finding is not the recommendation itself but the
cross-phase ordering: starter-only beats every pyramiding variant on this
dataset by a wide margin under correctly-attributed funding. If future data
upgrades the recommendation from NEEDS_MORE_DATA, the most likely target is
IMPLEMENT_STARTER_ONLY, not anything involving adds.

Forward action: paper-trade starter-only on 1d, collect bars from the next
distinct bull/bear cycle, and rerun this pipeline when effective sample size
exceeds ~20 trends. Do not retune parameters against the current 10-trend
sample.

## Best Ranked Config

- phase: `1`
- config_id: `p1_1d_local_stop1.5`
- timeframe: `1d`
- regime_mode: `local`
- trade_count: `9`
- net_return_pct: `18.837987610703085`
- max_drawdown_pct: `-11.713174143950745`
- win_rate: `22.22222222222222`
- fees_paid: `0.0`
- funding_pnl: `-115.03907122727423`

## Recommendation

`NEEDS_MORE_DATA`

## Phase 1 Sub-Window Robustness

| Window | Net Return % | Top Trade Contribution % | Genuinely Positive |
|---|---:|---:|---|
| W1 | 9.5548 | 1.0000 | False |
| W2 | -2.1419 | 0.0000 | False |
| W3 | 3.4384 | 1.0000 | False |

Note: top_trade_contribution saturates at 1.0 when a window contains exactly
one profitable trade. This indicates concentrated PnL within the window, not a
metric error. A window with two profitable trades of equal size would score
0.50; a window with one large winner and one small winner would score, e.g.,
0.85. Both W1 and W3 contain a single profitable trade in this run, so the
metric reaches its ceiling.

## Cross-Phase Comparison

Despite the NEEDS_MORE_DATA verdict, the relative ordering of strategy
complexity is consistent and worth recording for future reference:

| Phase | Best Config                      | Net Return % | Net / MaxDD | Trades |
|------:|----------------------------------|-------------:|------------:|-------:|
|     1 | starter-only, stop 1.5 ATR       |        18.84 |        1.61 |      9 |
|     2 | starter + 1 add @ 0.5 ATR        |        10.25 |        1.39 |      9 |
|     3 | starter + 2 adds, 0.4/0.35/0.25  |         8.62 |        1.39 |     11 |
|     4 | starter + runner (MA20)          |         2.69 |        0.97 |     19 |
|     5 | cost-adjusted baseline           |         1.79 |        0.64 |     23 |
|     7 | starter + cooldown 5             |         2.13 |        0.83 |     18 |

Observation: across every metric (net return, net-return-over-max-drawdown,
top-trade-contribution), starter-only dominates every variant that adds
complexity. Adding the first add lowers net return by ~8.6 percentage points;
adding the second add lowers it another 1.6 points; adding a runner cuts the
best remaining net return again.

Mechanism: the funding fix in this rerun made it visible that adds increase
open notional during exactly the high-funding bull phases the strategy aims to
ride. The marginal price capture from late-trend adds does not offset the
funding drag they introduce. This effect was masked in the previous (buggy)
run where funding was systematically underestimated for multi-lot phases.

Caveat: this ordering is observed on ~10 distinct MA100 bull episodes, two of
which dominate. The ordering is not statistically validated. It is, however,
the strongest directional signal this dataset provides, and it points away
from pyramiding on HYPE under current funding regime.

## Phase 6 Timeframe Sanity

| Timeframe | Regime      | Net Return % | Max Drawdown % | Single-Trade Dominated |
|----------:|-------------|-------------:|---------------:|:-----------------------|
|        1d | local       |        18.84 |         -11.71 | False (reference)      |
|        4h | daily_macro |        74.07 |         -16.01 | False                  |
|        1h | daily_macro |        63.09 |         -14.17 | True                   |

Sanity reference config: `p1_1d_local_stop1.5`

Divergence threshold: 15% absolute net_return_pct gap.

Sanity survived: `False`

Divergence direction: up. The 4h daily-macro result is materially higher than
the 1d reference result.

Interpretation:
- Because 4h net return is materially higher than 1d, the 1d "winner" may be
  underfitting and a finer execution timeframe may capture more of the trend.
  This argues for re-running with 4h as primary in future iterations.
- The conditional 1h sanity run also diverged upward, but it is
  single-trade dominated, so it should not be treated as a validated
  improvement.
- Either interpretation matters more than the boolean alone; in this run the
  failed sanity check means the 1d starter-only result is not stable across
  execution timeframe.

## Notes

- Position sizing is dollar-risk-at-stop based.
- Funding is applied from hourly funding rows while position is open.
- Configs with max drawdown above 40% or top-trade contribution above 60% are excluded from final ranking but kept in raw phase CSVs.
- v1.1 rankings are hypothesis generation, not statistical validation; sample size uses bull episodes, not bars or trades.
- Phase 1 positive-only-in-W3 downgrade active: `True`.
- 4h daily-macro sanity survived: `False`.
