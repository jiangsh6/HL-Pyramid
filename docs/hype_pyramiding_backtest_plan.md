# HYPE Pyramiding Backtest Plan

## Goal

Implement and test a complete HYPE/USDC perpetual pyramiding backtest for the HL Primiding project.

The goal is not to optimize blindly. The goal is to isolate whether each pyramiding component adds real value:

1. MA100 regime filter
2. Starter-only baseline
3. One-add pyramiding
4. Two-add pyramiding
5. Runner and giveback logic
6. Funding and fee-adjusted net return
7. Timeframe comparison
8. Re-entry cooldown

Primary asset: HYPE

Default market: Hyperliquid perpetual, HYPE/USDC

Important: use HYPE as the default research asset, not BTC.

---

## External Facts To Respect

Hyperliquid candle data should be pulled through the public info endpoint using candleSnapshot.

Funding must be applied using historical funding records. Hyperliquid funding is paid hourly, not once per candle. The backtest should align open positions to hourly funding timestamps and apply funding cost while the position is open.

Trading fees must be configurable. Do not hard-code one fee forever. Use a default taker fee config value, but make it easy to override.

---

## Implementation Requirements

Use pandas for:

- Data loading
- Candle normalization
- Indicator calculation
- Result aggregation

Use an explicit state-machine/event-loop backtest for trade simulation.

Do not force pure vectorization for execution logic. Pyramiding is stateful because it includes:

- Starter entry
- Add 1
- Add 2
- Stop migration
- Runner mode
- Peak PnL tracking
- Giveback stop
- Cooldown
- Funding accrual

A pure vectorized approach is likely to introduce subtle logic bugs.

---

## No-Lookahead Rules

Strictly enforce:

1. Indicators are computed only from candles available at signal time.
2. Signal decisions happen on candle close.
3. Entries execute at the next candle open.
4. Add triggers execute at the next candle open after the trigger candle.
5. MA/ATR/ADX values used for a signal must be shifted so the strategy does not use future data.
6. Stops may be modeled intrabar using candle high/low, but document the fill assumption clearly.
7. If both stop and add/TP are touched inside the same candle, use conservative ordering:
   - stop first for risk exits
   - then TP/add logic

---

## Data Setup

Create or reuse a data loader that pulls:

- 1d
- 4h
- 1h

For each candle, store:

- timestamp
- open
- high
- low
- close
- volume

Expected local output path:

text data/hyperliquid/HYPE_1d.csv data/hyperliquid/HYPE_4h.csv data/hyperliquid/HYPE_1h.csv data/hyperliquid/HYPE_funding.csv 

The loader should be idempotent:

- If file exists and --refresh is not passed, use local file.
- If --refresh is passed, re-pull and overwrite.
- Log start/end timestamps and number of rows.

---

## Indicators

For each timeframe, compute:

- MA20
- MA100
- ATR14
- ADX14
- rolling highest close since entry, during backtest only
- close above MA100 flag
- chop regime flags

Indicator columns:

text ma20 ma100 atr14 adx14 is_bull_local is_chop_adx is_chop_ma_band 

Definitions:

text is_bull_local = close > ma100 is_chop_adx = adx14 < 20 is_chop_ma_band = abs(close - ma100) <= atr14 for 5 consecutive candles 

Also support a daily macro regime mode:

text is_bull_daily = daily close > daily MA100 

When testing 1h or 4h, daily macro regime should be forward-filled into lower timeframe candles.

---

## Regime Modes To Test

Test two regime modes:

### Mode A — Local Timeframe Regime

For each timeframe:

text entry regime = close > local MA100 exit regime = close < local MA100 

### Mode B — Daily Macro Regime + Local Execution

For 1h and 4h:

text entry regime = daily close > daily MA100 exit regime = daily close < daily MA100 execution = local timeframe candles 

For 1d, Mode A and Mode B are effectively the same.

---

## Risk Model

Use dollar-risk sizing, not notional sizing.

Config defaults:

yaml initial_equity: 10000 max_thesis_risk_pct: 0.01 starter_stop_atr: 2.0 taker_fee_bps: 3.5 max_drawdown_exclusion_pct: 40 

For each thesis:

text max_thesis_risk_usd = current_equity * max_thesis_risk_pct 

Position size is calculated from stop distance:

text qty = allocated_risk_usd / abs(entry_price - stop_price) 

Do not interpret “60% starter” as 60% of account notional.

Correct interpretation:

text starter risk allocation = 60% of max thesis risk add 1 risk allocation = 40% of max thesis risk 

For multi-add:

text starter/add1/add2 risk allocations: 60/40 50/30/20 40/35/25 

At every add, recalculate:

- total quantity
- blended average entry
- global stop
- open risk to global stop
- remaining thesis risk budget
- unrealized PnL
- realized PnL
- fees
- funding

Block any add if:

text open_risk_after_add > max_thesis_risk_usd 

Also block any add if total trade PnL is not positive.

---

## State Machine

Implement clear trade states:

text FLAT STARTER_LONG ADD1_LONG ADD2_LONG RUNNER_LONG COOLDOWN EXITED 

Allowed transitions:

text FLAT -> STARTER_LONG STARTER_LONG -> ADD1_LONG ADD1_LONG -> ADD2_LONG STARTER_LONG -> RUNNER_LONG ADD1_LONG -> RUNNER_LONG ADD2_LONG -> RUNNER_LONG RUNNER_LONG -> EXITED EXITED -> COOLDOWN COOLDOWN -> FLAT 

Emergency exits can happen from any active long state:

text STOP_HIT MA100_BREAK GIVEBACK_STOP MA20_TRAIL CHANDELIER_TRAIL REGIME_FLIP END_OF_DATA 

---

## Phase 1 — Baseline, No Pyramiding

Purpose:

Isolate MA100 regime filter alpha.

Rules:

Entry:

text if regime is bull and current state is FLAT:     signal on candle close     enter long at next candle open 

Sizing:

text risk allocation = 100% of max thesis risk stop = entry - starter_stop_atr * ATR14 

Exit:

text global stop hit OR close < MA100 OR daily macro regime flips bearish if in daily macro mode 

No adds. No runner.

Output:

- trades
- win rate
- total return
- net return
- max drawdown
- average trade duration
- average winning trade
- average losing trade
- profit factor
- sample size warning if trades < 30

---

## Phase 2 — Starter + One Add

Purpose:

Test whether one add improves return versus Phase 1.

Rules:

Starter:

text starter risk allocation = 60% stop = entry - starter_stop_atr * ATR14 

Add 1 trigger:

Test:

text +0.5 ATR +1.0 ATR +1.5 ATR +2.0 ATR 

Trigger condition:

text close >= starter_entry + add1_trigger_atr * ATR14_at_starter AND current close > starter entry AND open risk after add <= max thesis risk AND trade unrealized PnL > 0 

Execution:

text Add signal on candle close Add fill at next candle open 

After Add 1:

text move global stop to starter entry 

Exit:

text global stop hit OR MA100 break OR daily macro regime flip 

Output additional fields:

- add1_hit_rate
- trades_with_add1
- trades_without_add1
- return_from_add1_trades
- return_from_non_add_trades
- average blended entry
- average exit price

---

## Phase 3 — Starter + Two Adds

Purpose:

Test whether second add improves return or hurts due to late-trend exhaustion.

Sizing grids:

text 50/30/20 40/35/25 

Add 1:

Use best Add 1 trigger from Phase 2, but also allow configurable override.

Add 2 trigger values:

text +1.5 ATR from starter +2.0 ATR from starter +2.5 ATR from starter 

Add 2 trigger condition:

text close >= starter_entry + add2_trigger_atr * ATR14_at_starter AND current close > blended average entry AND open risk after add <= max thesis risk AND trade unrealized PnL > 0 

After Add 2:

text global stop = add1_entry_price 

Exit:

text global stop hit OR MA100 break OR daily macro regime flip 

Output additional fields:

- add2_hit_rate
- trades_with_add2
- trades_without_add2
- return_from_add2_trades
- return_from_non_add2_trades
- average blended entry
- average exit price

Hypothesis to evaluate:

text On HYPE, Add 2 may have mixed or negative contribution because of fast reversals. 

---

## Phase 4 — Runner + Giveback Stop

Purpose:

Test whether partial TP and runner improves return versus full exit.

Activation:

After Add 1 or Add 2 is active, if trade reaches unrealized R threshold:

text +1.5R +2.0R +3.0R 

Then:

text sell 50% of total position remaining 50% becomes RUNNER_LONG 

Runner re-entry:

text None. 

Runner is a one-way wind-down state.

Runner exit methods to test:

### A. Giveback Stop

Track peak unrealized PnL after runner activation.

Test giveback thresholds:

text 25% 35% 50% 

Exit if:

text current_unrealized_pnl <= peak_unrealized_pnl * (1 - giveback_pct) 

### B. MA20 Trailing Stop

Exit if:

text close < MA20 

### C. Chandelier Exit

Exit if:

text close < highest_close_since_entry - 2 * ATR14 

Output additional fields:

- runner_activation_rate
- average runner duration
- average runner PnL
- runner PnL contribution
- runner exit reason distribution
- partial TP trigger used
- trailing method used

---

## Phase 5 — Funding + Fee Layer

Purpose:

Test whether the gross edge survives realistic trading costs.

Fees:

Apply configurable taker fee on every fill:

text fee = notional * taker_fee_bps / 10000 

Apply fees for:

- starter entry
- add entries
- partial TP
- final exit

Funding:

Load historical HYPE funding records.

Apply funding while position is open.

Funding should be applied using actual hourly funding timestamps, not candle-average approximation.

For each open position:

text funding_pnl = -position_notional_at_funding_timestamp * funding_rate 

For longs:

- positive funding rate is cost
- negative funding rate is income

If exact mark price is not available at funding timestamp, use nearest previous candle close or interpolated close. Document the assumption.

Output:

- gross return
- fee cost
- funding PnL
- net return
- return drag from fees
- return drag from funding
- trades flipped from profitable gross to losing net
- periods where funding materially hurt performance

Also test optional funding block:

text Block new long starters if hourly funding rate > threshold 

Thresholds:

text 0.005% 0.01% 0.02% 0.05% 

---

## Phase 6 — Timeframe Comparison

Purpose:

Find best timeframe empirically.

Run best configurations across:

text 1d 4h 1h 

For each timeframe, run both:

text local timeframe regime daily macro regime + local execution 

Compare:

- net return
- max drawdown
- number of trades
- average duration
- win rate
- profit factor
- return / max drawdown
- sample size reliability

Expected hypothesis:

text Daily: fewer trades, cleaner regime, lower noise. 1H: more trades, more noise, more fees/funding drag. 4H: possible sweet spot. 

Do not hard-code this conclusion. Let results decide.

---

## Phase 7 — Re-Entry Cooldown

Purpose:

Test whether cooldown after full exit reduces false re-entries.

After full exit:

text block new starter for N candles 

Test:

text 0 3 5 10 

Re-entry allowed only if:

text cooldown elapsed AND regime is bull AND close > MA100 

Output:

- net return by cooldown
- max drawdown by cooldown
- trade count by cooldown
- reduction in losing re-entries
- impact on missed winners

---

## Parameter Grid

Test the following:

| Parameter | Values |
|---|---|
| Timeframe | 1d, 4h, 1h |
| Regime mode | local, daily_macro |
| Starter stop ATR | 1.5, 2.0, 2.5 |
| Add 1 trigger ATR | 0.5, 1.0, 1.5, 2.0 |
| Add 2 trigger ATR | 1.5, 2.0, 2.5 |
| Size allocation | 100, 60/40, 50/30/20, 40/35/25 |
| Partial TP trigger | 1.5R, 2.0R, 3.0R |
| Giveback pct | 25%, 35%, 50% |
| Runner method | giveback, MA20, chandelier |
| Re-entry cooldown | 0, 3, 5, 10 |
| Chop filter | none, ADX<20, MA100_ATR_band |
| Funding block threshold | none, 0.005%, 0.01%, 0.02%, 0.05% |

Avoid exploding the full grid blindly. Run phase-by-phase and carry forward only reasonable candidate configurations.

---

## Output Files

Expected output directory:

text reports/research/hype/pyramiding_backtest_<timestamp>/ 

Required files:

text phase1_baseline.csv phase2_one_add.csv phase3_two_adds.csv phase4_runner.csv phase5_cost_adjusted.csv phase6_timeframe_comparison.csv phase7_cooldown.csv ranked_configs.csv backtest_report.md 

Optional files:

text equity_curve_phase1.png equity_curve_best.png drawdown_best.png trade_log_best.csv 

---

## Required Result Columns

Each phase summary should include:

text phase symbol timeframe regime_mode config_id trade_count sample_size_warning win_rate gross_return_pct net_return_pct max_drawdown_pct return_over_max_dd avg_trade_duration_bars avg_trade_duration_hours avg_win_pct avg_loss_pct profit_factor fees_paid funding_pnl add1_hit_rate add2_hit_rate runner_activation_rate cooldown_bars excluded_for_drawdown 

Exclude final-ranked configs where:

text max_drawdown_pct > 40 

But still save them in raw phase outputs.

---

## Trade Log Columns

For every trade, output:

text trade_id symbol timeframe regime_mode entry_time entry_price starter_qty starter_stop add1_time add1_price add1_qty add2_time add2_price add2_qty partial_tp_time partial_tp_price partial_tp_qty runner_start_time exit_time exit_price exit_reason final_qty_closed gross_pnl fees_paid funding_pnl net_pnl net_return_pct max_unrealized_pnl max_giveback_pct bars_held state_path 

---

## Testing Requirements

Add pytest coverage for:

1. Indicator no-lookahead behavior
2. ATR stop sizing
3. Dollar-risk position sizing
4. Add blocked when trade is not profitable
5. Add blocked when open risk exceeds thesis budget
6. Stop migration after Add 1
7. Stop migration after Add 2
8. Runner activation after R threshold
9. Giveback stop calculation
10. Chandelier stop calculation
11. MA100 exit
12. Daily macro regime forward-fill into lower timeframe
13. Funding applied only while position is open
14. Fees applied on every fill
15. Cooldown blocks re-entry
16. Final ranking excludes max drawdown above 40%

Expected test file:

text tests/test_hype_pyramiding_backtest.py 

---

## Suggested Scripts

Create:

text scripts/fetch_hype_hl_data.py scripts/run_hype_pyramiding_backtest.py 

Example commands:

bash python scripts/fetch_hype_hl_data.py --symbol HYPE --refresh python scripts/run_hype_pyramiding_backtest.py --symbol HYPE --all-phases pytest tests/test_hype_pyramiding_backtest.py -q pytest tests/ -q 

---

## Backtest Report Requirements

backtest_report.md should include:

1. Data coverage summary
2. Phase-by-phase result summary
3. Best baseline config
4. Best one-add config
5. Best two-add config
6. Whether Add 2 helped or hurt
7. Best runner method
8. Gross vs net cost impact
9. Funding drag analysis
10. Best timeframe conclusion
11. Cooldown conclusion
12. Final ranked configs
13. Explicit warning if sample size is too small
14. Recommendation for whether to implement in live HL Primiding

Final recommendation should use one of:

text DO_NOT_IMPLEMENT IMPLEMENT_STARTER_ONLY IMPLEMENT_STARTER_PLUS_ONE_ADD IMPLEMENT_STARTER_PLUS_TWO_ADDS IMPLEMENT_RUNNER_ONLY_AFTER_PARTIAL_TP NEEDS_MORE_DATA 

---

## Acceptance Criteria

The implementation is complete only if:

1. All phases run successfully.
2. Each phase writes a CSV result file.
3. ranked_configs.csv is generated.
4. backtest_report.md is generated.
5. Full trade log for the best config is generated.
6. Funding and fees are included in Phase 5 onward.
7. Timeframe comparison includes 1d, 4h, and 1h.
8. Both local and daily macro regime modes are tested.
9. No-lookahead tests pass.
10. Risk-budget tests pass.
11. pytest tests/ -q passes.
12. The final report gives a clear implementation recommendation.

---

## Important Research Bias

Do not assume pyramiding is good.

The backtest must be willing to conclude:

text Starter-only beats pyramiding. One add is useful but two adds are harmful. Runner improves convexity but worsens drawdown. Funding destroys the edge. Daily timeframe is better despite fewer trades. 1H has too much noise. 4H is the best compromise. 

Let the data decide.

---

## v1.1 Mandatory Overrides

These v1.1 rules supersede any conflicting earlier section. They are mandatory and are designed to reduce overfitting risk from HYPE's small effective trend sample.

### Single-Trade Dominance Exclusion

Every config must compute:

```text
top_trade_contribution_pct = max(single positive trade net_pnl) / sum(all positive trade net_pnl)
single_trade_dominated = top_trade_contribution_pct > 0.60
```

Final ranked configs must exclude any config where:

```text
single_trade_dominated = true
```

Add required columns to every phase CSV and ranked config output:

```text
single_trade_dominated
top_trade_contribution_pct
exclusion_reason
```

If a config is excluded by both drawdown and single-trade dominance, `exclusion_reason` should include both reasons.

### Revised Timeframe Logic

Default backtest scope:

1. Primary candidate testing: `1d`.
2. Sanity check: `4h` with `daily_macro` regime only, using the best carried-forward 1d config family.
3. Skip `1h` by default.

Run `1h` only if the 4h daily-macro sanity result diverges from the 1d result by more than 15% net return.

This overrides the earlier broad default requirement to always run 1d, 4h, and 1h.

### Carry Forward Exactly One Config Per Phase

Each phase should select one winner and carry only that configuration family forward.

Selection metric:

```text
net_return_over_max_dd
```

Eligibility:

- Not single-trade dominated.
- Not excluded for max drawdown.

Tie-break rule:

If candidates are within approximately 5% of each other on `net_return_over_max_dd`, choose the simpler config:

1. Fewer adds.
2. No runner.
3. Longer timeframe.

Raw candidate results still belong in phase CSVs, but only one config family advances.

### Phase 1 Sub-Window Robustness

Phase 1 must report robustness across:

```text
W1: listing -> 2025-06-30
W2: 2025-07-01 -> 2026-01-31
W3: 2026-02-01 -> end
```

Add columns:

```text
w1_net_return_pct
w2_net_return_pct
w3_net_return_pct
positive_only_in_w3
```

If positive return appears only in W3, final recommendation must be downgraded to:

```text
NEEDS_MORE_DATA
```

### Ranking Metric

Rank by:

```text
net_return_over_max_dd
```

Never rank by net return alone.

Secondary tie-breaks:

1. Higher win rate.
2. Fewer state transitions / simpler config.

Phase 4 runner configs must beat the Phase 1 baseline on net `net_return_over_max_dd`, not gross return.

### Effective Sample Size

Sample size is not bar count and not trade count.

Compute:

```text
effective_sample_size = number of distinct bull episodes
```

where a bull episode is a contiguous block where the active regime is bullish.

Set:

```text
sample_size_warning = true
```

when:

```text
effective_sample_size < 30
```

Reports must state clearly that rankings are hypothesis generation, not statistical validation.

### Recommendation Constraints

Do not emit:

```text
IMPLEMENT_STARTER_PLUS_TWO_ADDS
```

Allowed recommendations are only:

```text
DO_NOT_IMPLEMENT
NEEDS_MORE_DATA
IMPLEMENT_STARTER_ONLY
IMPLEMENT_STARTER_PLUS_ONE_ADD
IMPLEMENT_RUNNER_ONLY_AFTER_PARTIAL_TP
```

Anything above starter-only must pass all of:

1. Not single-trade dominated.
2. Net post-funding result beats starter-only on `net_return_over_max_dd`.
3. Survives the 4h daily-macro sanity check.

### Unchanged Requirements

The following original requirements remain unchanged:

- Dollar-risk sizing.
- Hourly funding alignment.
- Block add if trade PnL is not positive.
- Block add if open risk exceeds thesis budget.
- Giveback runner design.
- No-lookahead rules.
- Conservative same-candle stop-first ordering.
- Willingness to conclude starter-only wins.
