# ARCHIVED PRE-HL EQUITY SPEC — Long Thesis Pyramiding Bot — Final Implementation Spec

This file is retained as a historical implementation archive. Runtime code now
uses Hyperliquid perpetual quantity semantics (`qty`, `current_position_qty`,
`lot.qty`) and must not use equity share semantics.
### Version: v0.2.1-final | For use with: Claude Code

> **Lineage:** v0.1 (original) → v0.2 (14 engineering fixes) → v0.2.1 (12 additional patches)
> **Status:** Implementation-ready. This document is the single source of truth.
> **Scope:** Paper trading only. No live execution in v0.1.

---

## HOW TO USE THIS DOCUMENT (Claude Code Instructions)

1. Read this entire document before writing a single line of code.
2. Implement **Phase 1 only** (Section 25.1). Stop at the Phase 1 gate.
3. Run `pytest tests/test_config.py tests/test_state_machine.py tests/test_indicators.py -q`
4. Do not proceed to Phase 2 until Phase 1 tests pass with zero failures.
5. Every section is final. Do not invent behavior not described here.
6. Section 25 specifies exact files to create per phase.
7. Section 29 gives Phase 1 model schemas as implementation reference.

---

## 0. Purpose

Build a single-ticker, long-only thesis position manager.

This bot does **not** predict price direction. A human provides the bullish thesis, ticker,
event date, and risk budget. The bot manages entry, base sizing, pyramiding add-ons, take
profit, reduce logic, stop loss, event-risk de-risking, paper execution, logging, and
backtesting.

Example thesis:
```yaml
ticker: MU
thesis: "Bullish MU into next earnings"
event_date: "2026-06-24"
target_price: 2000
```

The bot's job:
> If the thesis is right, scale into the trend.
> If the trend weakens, reduce add-ons first.
> If risk limits are hit, exit.
> Before earnings, de-risk.

---

## 1. Core Philosophy

```
Human decides thesis. Bot manages risk and position.
```

The bot must **never**:
- Short the ticker
- Average down losing positions
- Add when unrealized PnL is negative
- Add when price is below trend filters
- Add near earnings/event window
- Ignore stop loss because target price is higher
- Use unlimited leverage
- Re-enter automatically after a thesis-level halt

The bot must **always**:
- Start small; add only after price confirms the thesis
- Use LIFO reduce logic for add-ons
- Protect realized and unrealized profit
- De-risk before binary events
- Log every decision, including no-action cycles

---

## 2. MVP Scope

### 2.1 Included in v0.1

- Single ticker, long-only
- Daily bar based decision engine
- Intraday monitoring for exits/halts only (scope: Section 8.2)
- Paper trading (no live execution)
- YAML config with schema validation
- State machine (9 states + protect_profit_mode flag)
- Entry, add, reduce, take-profit, stop, event-risk logic
- CSV logs, JSON state persistence
- Backtest engine (Phase 4)
- Unit tests (all test names specified in Section 22)
- Daily summary output

### 2.2 Excluded from v0.1

- Multi-ticker portfolio optimization
- Options, short selling, intraday scalping
- Real money live trading
- AI news interpretation or automatic thesis generation
- Telegram notifications (config placeholder only, never called)
- `down_volume` filter (reserved for v0.3)
- Full adjusted OHLC construction (v0.3)

---

## 3. System Architecture

```
long_thesis_bot/
  config/
    mu_long_thesis.yaml

  src/
    data/
      market_data.py          fetch_ohlcv() via yfinance
      indicators.py           calc_indicators(df, cfg) → IndicatorSnapshot

    core/
      models.py               All dataclasses/Pydantic models (Section 29)
      config_loader.py        load_config(), validate_config()
      state_machine.py        BotState enum, valid transitions, StateMachine
      decision_engine.py      Priority-ordered orchestrator → Decision

    strategy/
      entry.py                check_starter_entry, check_pullback_base, check_breakout_base
      add.py                  check_add_conditions
      reduce.py               check_addon_reduce, check_base_reduce, lifo_reduce()
      take_profit.py          layered_tp, target_tp, exposure_tp, giveback_tp, trailing_tp
      stops.py                calc_initial_stop, update_trailing_stop, check_stop_triggers
      event_risk.py           calc_trading_days_to_event, check_event_risk_mode
      sizing.py               calc_entry_size (10.1), calc_addon_size (10.2)

    execution/
      paper_broker.py         execute(decision, fill_ref_price, state) → Fill  [stateless]
      order_manager.py        Order lifecycle helpers

    risk/
      risk_manager.py         Pre-trade risk gate
      validators.py           Data and state validation

    reporting/
      logger.py               CSV writers: orders, positions, risk, decisions
      state_writer.py         Serialize/deserialize ThesisState ↔ state.json
      daily_summary.py        Formatted daily summary text

    backtest/
      backtester.py           Event loop over historical bars
      metrics.py              All 19 metrics
      charts.py               matplotlib output

  scripts/
    run_backtest.py
    run_paper.py
    run_daily_decision.py
    generate_daily_summary.py
    reset_state.py            Manual HALTED/EXITED reset (requires --confirm)

  tests/
    test_config.py
    test_state_machine.py
    test_indicators.py
    test_entry_logic.py
    test_add_logic.py
    test_reduce_logic.py
    test_take_profit.py
    test_stops.py
    test_event_risk.py
    test_sizing.py
    test_paper_broker.py
    test_decision_engine.py
```

---

## 4. State Machine

### 4.1 States (9 total — PROTECT_PROFIT is NOT a state)

```
FLAT
  No position. Waiting for entry.

STARTER_LONG
  Small starter position opened. Trend confirmation is incomplete.

BASE_LONG
  Core thesis position established. No add-on lots yet.

PYRAMID_LONG
  Base position plus one or more active add-on lots.
  (State remains PYRAMID_LONG when additional lots are added.)

REDUCE_MODE
  Trend has weakened. Add-ons being reduced first, then base if needed.
  Adds are blocked while in this state.

EVENT_RISK_MODE
  Binary event window is near. New entries and adds are blocked.
  De-risking orders fire per T-5/T-2/T-1 thresholds (Section 15).

RUNNER_LONG
  Post-target-TP state. Only a small runner position remains.
  Adds are blocked. Tight trailing stop active.

EXITED
  Position fully closed. Requires reset_state.py --confirm to return to FLAT.

HALTED
  Risk-level halt (max loss, data error, state mismatch).
  No new orders until reset_state.py --confirm.
```

**protect_profit_mode (boolean flag — NOT a state):**

```
protect_profit_mode is stored in state.json as a bool alongside the main state.

Set to True when ANY of:
  - add_count reaches max_add_count
  - unrealized profit from avg_entry_price >= 30% (TP level-1 threshold)

While True:
  - New add orders are blocked (decision engine Step 12 skipped)
  - Current state (BASE_LONG or PYRAMID_LONG) is unchanged
  - All reduce, TP, and stop logic operates normally

Cleared to False when:
  - post_event.reset_add_count fires after event cooldown
  - Manual reset_state.py --confirm is run

Rule: protect_profit_mode is NOT auto-cleared when profit drops back below 30%.
      Once set during a thesis, it stays until the above clearing conditions.
      This prevents add/protect oscillation on volatile bars near the threshold.
```

### 4.2 State Transitions — Complete Table

**Entry transitions:**
```
FLAT → STARTER_LONG
  Condition: starter entry conditions met (Section 9.1)

FLAT → BASE_LONG
  Condition: pullback_base or breakout_base conditions met while in FLAT
  Note: FLAT→BASE_LONG is valid. Starter entry is optional, not mandatory.

STARTER_LONG → BASE_LONG
  Condition: pullback_base or breakout_base conditions met

BASE_LONG → PYRAMID_LONG
  Condition: add conditions met (Section 12); adds first lot

PYRAMID_LONG → PYRAMID_LONG
  Condition: subsequent add conditions met (add_count < max_add_count)
  Note: Self-transition; state does not change name, add_count increments.
```

**Protect profit flag transitions (no state change):**
```
Set protect_profit_mode = True (not a state transition):
  Condition: add_count reaches max_add_count
             OR unrealized profit from avg_entry_price >= 30%
  Effect: adds blocked; state (BASE_LONG or PYRAMID_LONG) unchanged

Clear protect_profit_mode = False:
  Condition: post-event cooldown fires with reset_add_count = true
             OR manual reset_state.py --confirm is run
```

**Reduce transitions:**
```
PYRAMID_LONG → REDUCE_MODE
  Condition: short-term trend break or drawdown from peak exceeds addon_reduce threshold

BASE_LONG → REDUCE_MODE
  Condition: medium-term trend break or drawdown from peak exceeds base_reduce threshold

REDUCE_MODE → BASE_LONG
  Condition: all add-on lots removed; base position intact; no further reduce trigger active

REDUCE_MODE → EXITED
  Condition: base position fully exited (Rule F: close < MA50 fires while in REDUCE_MODE)
```

**Event risk transitions:**
```
ANY_LONG_STATE → EVENT_RISK_MODE
  Condition: trading_days_to_event <= stop_adding_trading_days_before (default: 10)
  ANY_LONG_STATE = {STARTER_LONG, BASE_LONG, PYRAMID_LONG, REDUCE_MODE}
  Action: save prior_state to state.prior_state before entering

EVENT_RISK_MODE → [prior_state restored]
  Condition: event has passed AND post_event.cooldown_trading_days elapsed
  Action: restore state = state.prior_state; clear prior_state
  If prior_state unavailable: restore BASE_LONG if position > 0, else FLAT
```

**Runner mode transitions:**
```
ANY_LONG_STATE → RUNNER_LONG
  Condition: target_price TP fires AND runner_mode.enabled = true (Section 14.2)
  ANY_LONG_STATE = {STARTER_LONG, BASE_LONG, PYRAMID_LONG, REDUCE_MODE, EVENT_RISK_MODE}
  Note: Target TP can be hit from any state with an open position.

RUNNER_LONG → EXITED
  Condition: runner trailing stop hit
```

**Exit and halt transitions:**
```
ANY_LONG_STATE → EXITED
  Condition: hard stop, trailing stop, Rule F (close < MA50), or force-flat event rule

ANY_STATE → HALTED
  Condition: max_thesis_loss exceeded, severe data validation failure,
             or critical state mismatch

HALTED → FLAT    (manual reset only)
  Requires: python scripts/reset_state.py --confirm
  Effect: clears position, clears all lots, resets PnL counters, state = FLAT

EXITED → FLAT    (manual reset only)
  Requires: same reset_state.py --confirm command
```

**Valid transitions set (for StateMachine implementation):**
```python
VALID_TRANSITIONS = {
    "FLAT":             {"STARTER_LONG", "BASE_LONG", "HALTED"},
    "STARTER_LONG":     {"BASE_LONG", "EVENT_RISK_MODE", "EXITED", "HALTED"},
    "BASE_LONG":        {"PYRAMID_LONG", "REDUCE_MODE", "EVENT_RISK_MODE",
                         "RUNNER_LONG", "EXITED", "HALTED"},
    "PYRAMID_LONG":     {"PYRAMID_LONG", "REDUCE_MODE", "EVENT_RISK_MODE",
                         "RUNNER_LONG", "EXITED", "HALTED"},
    "REDUCE_MODE":      {"BASE_LONG", "EVENT_RISK_MODE", "RUNNER_LONG",
                         "EXITED", "HALTED"},
    "EVENT_RISK_MODE":  {"STARTER_LONG", "BASE_LONG", "PYRAMID_LONG",
                         "REDUCE_MODE", "RUNNER_LONG", "EXITED", "HALTED"},
    "RUNNER_LONG":      {"EXITED", "HALTED"},
    "EXITED":           {"FLAT"},   # manual reset only
    "HALTED":           {"FLAT"},   # manual reset only
}
```

---

## 5. YAML Configuration

`config/mu_long_thesis.yaml` — canonical sample:

```yaml
bot:
  name: long_thesis_pyramiding_bot
  version: "0.1"
  mode: paper                        # ONLY valid value in v0.1 — "live" is rejected
  timezone: America/New_York

symbol:
  ticker: MU
  asset_type: equity
  direction: long_only
  allow_short: false

thesis:
  enabled: true
  thesis_name: "MU AI memory / HBM momentum into earnings"
  thesis_start_date: "2026-05-27"
  event_date: "2026-06-24"           # SINGLE SOURCE OF TRUTH for event date
  target_price: 2000                 # SINGLE SOURCE OF TRUTH for target price
  manual_override_required_after_expiry: true

capital:
  starting_equity: 100000
  max_total_capital_at_risk_pct: 0.06
  max_symbol_exposure_pct: 1.00
  max_initial_exposure_pct: 0.20
  max_starter_exposure_pct: 0.08
  max_addon_exposure_pct: 0.30
  reserve_cash_pct: 0.25

leverage:
  enabled: false
  max_leverage: 1.0
  max_leverage_with_profit_cushion: 1.5
  profit_cushion_required_pct: 0.10
  earnings_week_max_leverage: 1.0
  force_deleverage_before_event: true

entry:
  mode: starter_pullback_breakout

  starter:
    enabled: true
    exposure_pct: 0.08
    require_thesis_enabled: true
    require_close_above_ma20: true
    max_distance_above_ma10_pct: 0.10
    max_distance_above_ma20_pct: 0.15
    max_intraday_gain_pct: 0.08
    min_volume_vs_20d_avg: 0.80
    max_gap_up_pct: 0.06

  pullback_base:
    enabled: true
    additional_exposure_pct: 0.12
    require_close_above_ma20: true
    require_ma20_above_ma50: true
    min_drawdown_from_20d_high_pct: 0.03
    max_drawdown_from_20d_high_pct: 0.08
    require_reclaim_ma5: true
    require_reclaim_ma10: false
    max_down_volume_vs_20d_avg: 1.50  # Reserved for v0.3 — NOT evaluated in v0.1

  breakout_base:
    enabled: true
    additional_exposure_pct: 0.12
    require_close_above_20d_high: true
    require_close_above_ma10: true
    require_ma10_above_ma20: true
    require_ma20_above_ma50: true
    min_volume_vs_20d_avg: 1.50
    max_distance_above_ma10_pct: 0.08
    max_intraday_gain_pct: 0.10

add:
  enabled: true
  max_add_count: 4
  min_days_after_entry_before_first_add: 2
  min_days_between_adds: 1
  min_unrealized_profit_before_first_add_pct: 0.04
  add_trigger_type: price_step
  add_trigger_pct: 0.05
  add_sizes_pct:         # Must have exactly max_add_count elements
    - 0.08
    - 0.06
    - 0.04
    - 0.02
  require_close_above_ma10: true
  require_ma10_above_ma20: true
  require_price_above_last_add_price: true
  forbid_add_if_intraday_gain_above_pct: 0.10
  forbid_add_if_distance_above_ma10_pct: 0.12
  forbid_add_if_distance_above_ma20_pct: 0.20
  stop_adding_if_event_within_trading_days: 10

reduce:
  enabled: true
  addon_reduce:
    reduce_latest_add_if_close_below_ma5: true
    reduce_all_addons_if_close_below_ma10: true
    reduce_all_addons_if_drawdown_from_peak_pct: 0.10
  base_reduce:
    reduce_base_half_if_close_below_ma20: true
    reduce_base_half_if_drawdown_from_peak_pct: 0.15
    reduce_base_all_if_close_below_ma50: true

take_profit:
  enabled: true
  mode: hybrid_lifo

  layered_profit:
    enabled: true
    levels:
      - trigger_profit_pct_from_avg_entry: 0.30
        reduce_pct_of_total_position: 0.10
        reduce_priority: addons_first
        move_stop_to: breakeven
      - trigger_profit_pct_from_avg_entry: 0.50
        reduce_pct_of_total_position: 0.15
        reduce_priority: addons_first
        move_stop_to: lock_20pct_profit
      - trigger_profit_pct_from_avg_entry: 0.75
        reduce_pct_of_total_position: 0.20
        reduce_priority: addons_first
        move_stop_to: lock_35pct_profit
      - trigger_profit_pct_from_avg_entry: 1.00
        reduce_pct_of_total_position: 0.25
        reduce_priority: addons_first
        move_stop_to: lock_50pct_profit

  target_price:
    enabled: true
    # target_price read from thesis.target_price — NOT duplicated here
    reduce_pct_of_remaining_position: 0.50
    final_action: enter_runner_mode

  runner_mode:
    enabled: true
    runner_position_pct_of_original_base: 0.25
    trailing_stop_pct: 0.08

  exposure_take_profit:
    enabled: true
    soft_exposure_cap_pct: 0.80
    hard_exposure_cap_pct: 1.00
    reduce_to_exposure_pct: 0.70

  profit_giveback:
    enabled: true
    tiers:
      - min_unrealized_profit_pct_of_equity: 0.05
        max_giveback_pct: 0.50
      - min_unrealized_profit_pct_of_equity: 0.10
        max_giveback_pct: 0.35
      - min_unrealized_profit_pct_of_equity: 0.20
        max_giveback_pct: 0.25

  trailing_take_profit:
    enabled: true
    tiers:
      - min_profit_pct_from_avg_entry: 0.20
        trailing_pct: 0.12
        action: reduce_addons_50pct
      - min_profit_pct_from_avg_entry: 0.50
        trailing_pct: 0.10
        action: reduce_all_addons
      - min_profit_pct_from_avg_entry: 1.00
        trailing_pct: 0.08
        action: reduce_to_core

risk:
  initial_stop:
    type: max_of_pct_or_atr
    stop_pct: 0.12
    atr_multiplier: 2.5

  trailing_stop:
    enabled: true
    default_trailing_pct: 0.12
    profit_tiers:
      - min_profit_pct: 0.20
        trailing_pct: 0.10
      - min_profit_pct: 0.50
        trailing_pct: 0.08
      - min_profit_pct: 1.00
        trailing_pct: 0.06

  max_loss:
    max_thesis_loss_pct_of_equity: 0.06
    max_daily_loss_pct_of_equity: 0.03
    max_intraday_drawdown_pct_of_equity: 0.04

  no_trade_conditions:
    max_intraday_gain_for_new_entry_pct: 0.10
    max_intraday_loss_before_halt_pct: 0.10
    max_gap_down_before_halt_pct: 0.08
    max_spread_bps: 30
    min_avg_daily_dollar_volume: 100000000

event_risk:
  enabled: true
  # event_date read from thesis.event_date — NOT duplicated here
  stop_new_entry_trading_days_before: 10
  stop_adding_trading_days_before: 10
  reduce_to_max_exposure_trading_days_before: 5
  max_exposure_before_event_pct: 0.50
  reduce_to_core_trading_days_before: 2
  core_exposure_before_event_pct: 0.20
  force_flat_before_event: false
  force_flat_trading_days_before: 1
  post_event:
    cooldown_trading_days: 1
    require_new_trend_confirmation: true
    reset_add_count: true

execution:
  broker: paper
  order_type: limit
  limit_offset_bps: 10
  max_slippage_bps: 25
  allow_market_order: false
  market_order_allowed_for_stop_exit: true
  order_timeout_seconds: 60      # Live broker only — ignored in paper/backtest
  retry_count: 2
  retry_delay_seconds: 5
  partial_fill_policy: accept_partial_then_reprice
  round_lot_size: 1

data:
  source: yfinance               # v0.1 only valid value
  bar_interval: 1d
  intraday_check_interval_minutes: 15
  use_adjusted_close: true
  required_history_days: 120
  indicators:
    ma_windows: [5, 10, 20, 50]
    atr_window: 14
    volume_window: 20
    high_breakout_window: 20

logging:
  run_dir: reports/long_thesis_bot
  write_orders_csv: true
  write_positions_csv: true
  write_risk_csv: true
  write_state_json: true
  write_daily_summary: true

notifications:
  enabled: false
  telegram_enabled: false         # Placeholder only — never called in v0.1
  notify_on:
    - entry
    - add
    - reduce
    - take_profit
    - stop
    - event_risk_mode
    - halt
    - daily_summary
```

### 5.1 Config Validation Rules

`config_loader.validate_config()` must enforce all of these. Raise `ValueError` on any violation:

```
1. bot.mode must equal "paper" — reject "live" explicitly
2. thesis.event_date must be a valid ISO date string if event_risk.enabled = true
3. thesis.target_price must be > 0
4. add.add_sizes_pct must have exactly add.max_add_count elements
5. All exposure percentages must be in range (0.0, 1.0] inclusive of 1.0
6. data.source must equal "yfinance" in v0.1
7. event_risk block must NOT contain event_date (read from thesis.event_date)
8. take_profit block must NOT contain target_price (read from thesis.target_price)
9. capital.max_total_capital_at_risk_pct must be in range (0.0, 1.0)
```

---

## 6. Data Requirements

### 6.1 Market Data

**Raw fields from yfinance:**
```
date           DatetimeIndex (UTC-normalized)
open           float — raw, unadjusted
high           float — raw, unadjusted
low            float — raw, unadjusted
close          float — raw, unadjusted
adj_close      float — adjusted for splits and dividends
volume         int
```

**Derived indicators (computed in `indicators.py`):**
```
ma5                  rolling mean(adj_close, 5)
ma10                 rolling mean(adj_close, 10)
ma20                 rolling mean(adj_close, 20)
ma50                 rolling mean(adj_close, 50)
atr14                Average True Range, window=14, on raw OHLC
avg_volume_20d       rolling mean(volume, 20)
prior_highest_high_20d   rolling max(high, 20).shift(1)
                         SHIFTED BY 1 BAR to avoid look-ahead bias.
                         Today's value = yesterday's 20-day rolling high max.
drawdown_from_20d_high   = (prior_highest_high_20d - adj_close) / prior_highest_high_20d
distance_from_ma10   = (adj_close - ma10) / ma10
distance_from_ma20   = (adj_close - ma20) / ma20
intraday_return      = (adj_close - bar.open) / bar.open  [uses raw open]
gap_up_pct           = max(0, (bar.open - prev_bar.adj_close) / prev_bar.adj_close)
gap_down_pct         = max(0, (prev_bar.adj_close - bar.open) / prev_bar.adj_close)
prev_adj_close       prior bar's adj_close (convenience field)
```

**v0.1 OHLC Usage Note:**

```
Use adj_close for:
  - All MA and ATR calculations
  - drawdown_from_20d_high, distance_from_MA10/MA20
  - All profit / loss / stop calculations
  - Backtest return metrics

Use raw open for:
  - intraday_return, gap_up_pct, gap_down_pct
  - paper_broker fill price (next_bar.open)

Use raw high for:
  - highest_price_since_entry updates (trailing stop, Section 16.2)
  - prior_highest_high_20d computation

Known v0.1 limitation: mixing adj_close with raw open/high can distort intraday
and gap calculations on pre-split historical bars. Acceptable for v0.1 tickers
(MU, NVDA, etc., recent years). Full adjusted OHLC deferred to v0.3.

down_volume is NOT computed in v0.1 (max_down_volume_vs_20d_avg config field
is reserved but never evaluated).
```

### 6.2 Data Validation

Run before every decision cycle. Failure severity:

```
WARN (no trade, no halt):
  - Volume below min_avg_daily_dollar_volume
  - Stale bar (data > 1 trading day old in live/paper-live mode)

HALT (set state = HALTED, require manual reset):
  - Any indicator column is NaN on the latest bar
  - adj_close is missing, zero, or negative
  - state.json is corrupted or fails schema validation
  - Config schema validation fails
```

### 6.3 Data Source: yfinance

```python
import yfinance as yf

def fetch_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    """
    Returns DataFrame with columns: open, high, low, close, adj_close, volume
    Index: DatetimeIndex (date only, UTC-normalized).
    Requires at least config.data.required_history_days of bars.
    """
    df = yf.download(ticker, start=start, end=end, auto_adjust=False, progress=False)
    df.columns = [c.lower().replace(" ", "_") for c in df.columns]
    df = df.rename(columns={"adj_close": "adj_close"})
    return df[["open", "high", "low", "close", "adj_close", "volume"]]
```

---

## 7. State Persistence

### 7.1 state.json — Complete Field Specification

```json
{
  "symbol": "MU",
  "state": "PYRAMID_LONG",
  "prior_state": "BASE_LONG",
  "protect_profit_mode": false,
  "runner_mode_active": false,
  "runner_target_shares": null,

  "thesis_enabled": true,
  "entry_date": "2026-05-27",

  "base_lot": {
    "lot_id": "base",
    "entry_price": 900.0,
    "shares": 22,
    "entry_date": "2026-05-27"
  },
  "addon_lots": [
    {
      "lot_id": "add_1",
      "entry_price": 945.0,
      "shares": 8,
      "entry_date": "2026-05-29"
    },
    {
      "lot_id": "add_2",
      "entry_price": 1000.0,
      "shares": 6,
      "entry_date": "2026-06-02"
    }
  ],

  "avg_entry_price": 924.06,
  "current_position_shares": 36,
  "current_notional": 32400.0,

  "add_count": 2,
  "last_add_price": 1000.0,

  "highest_price_since_entry": 1100.0,
  "peak_unrealized_pnl_pct": 0.12,
  "initial_stop_price": 792.0,
  "trailing_stop_price": 990.0,

  "tp_levels_triggered": [false, false, false, false],
  "target_price_tp_triggered": false,

  "realized_pnl": 0.0,
  "unrealized_pnl": 9000.0,
  "thesis_pnl": 9000.0,
  "days_to_event": 15,

  "halted": false,
  "halt_reason": null,
  "last_action": "add",
  "last_updated": "2026-06-02T16:00:00"
}
```

**Field notes:**
- `base_lot` and each element of `addon_lots` share the same `LotRecord` schema.
- `base_lot` is reduced only by base_reduce rules (Section 13.2). Never by LIFO add-on reduce.
- `avg_entry_price` is a cached computed field — must be recalculated after every fill.
- `tp_levels_triggered[0..3]` maps to layered TP levels at [30%, 50%, 75%, 100%].
- `target_price_tp_triggered` is independent of `tp_levels_triggered`.
- `runner_target_shares` is set when entering RUNNER_LONG; null otherwise.
- `prior_state` is set when entering EVENT_RISK_MODE; restored on exit.

### 7.2 avg_entry_price Recalculation

After every fill (add or reduce), recalculate:

```
all_lots = [base_lot] + addon_lots     (only lots with shares > 0)

avg_entry_price = (
    sum(lot.shares * lot.entry_price for lot in all_lots)
    / sum(lot.shares for lot in all_lots)
)
```

When LIFO reduces remove add-on lots, `avg_entry_price` drops (earlier lots have lower cost).
When `base_lot.shares` is reduced, recalculate from remaining base + remaining add-ons.

### 7.3 Persistence Rules

```
- state.json is written after every decision cycle (including no-action cycles).
- state.json is written immediately after every fill.
- If state.json cannot be written, halt the bot.
- state.json is the SINGLE SOURCE OF TRUTH in paper mode.
- paper_broker does NOT maintain its own position state. It is stateless.
  (See Section 18.1 for paper_broker architecture.)
- On startup: load state.json, validate schema, check internal consistency.
  If current_position_shares != base_lot.shares + sum(a.shares for a in addon_lots):
      Set state = HALTED, halt_reason = "state_consistency_error"
```

---

## 8. Decision Engine Priority

### 8.1 Daily Decision Cycle — Canonical 13-Step Order

Every EOD run follows this order exactly. Earlier steps that produce a terminal action
(EXIT, HALT) cause later steps to be skipped for that cycle.

```
Step 1  — Load and validate data, config, state.json (Section 6.2)
           Halt if HALT-severity validation failure.

Step 2  — Calculate indicators for the current bar (all fields from Section 6.1)

Step 3  — Reconcile (paper mode):
           Verify state.json internal consistency:
           current_position_shares == base_lot.shares + sum(addon_lots[].shares)
           If mismatch → state = HALTED, halt_reason = "state_consistency_error" → stop

Step 4  — UPDATE TRAILING STOP (MUST happen before any trigger checks):
           4a. highest_price_since_entry = max(state.highest_price_since_entry, bar.high)
           4b. profit_from_avg = (adj_close - avg_entry_price) / avg_entry_price
               trailing_pct = default_trailing_pct
               for tier in profit_tiers (highest min_profit_pct first):
                   if profit_from_avg >= tier.min_profit_pct:
                       trailing_pct = tier.trailing_pct; break
           4c. candidate_stop = highest_price_since_entry * (1 - trailing_pct)
           4d. trailing_stop_price = max(state.trailing_stop_price, candidate_stop)
               [trailing stop is NEVER lowered]
           4e. Persist updated values before proceeding.
           Note: Only runs when current_position_shares > 0.

Step 5  — Update EVENT_RISK_MODE status — state transition ONLY, NO orders:
           If trading_days_to_event <= stop_adding_trading_days_before
              AND state not already EVENT_RISK_MODE:
               save prior_state = current state
               state = EVENT_RISK_MODE
           If state == EVENT_RISK_MODE AND event has passed
              AND post_event cooldown elapsed:
               restore state = prior_state; clear prior_state
               if reset_add_count: add_count = 0; clear protect_profit_mode

Step 6  — Check hard stops and max loss (terminal if exit/halt):
           If adj_close <= initial_stop_price:
               EXIT ALL → state = EXITED → stop
           If thesis_pnl / starting_equity <= -max_thesis_loss_pct_of_equity:
               EXIT ALL → state = HALTED, halt_reason = "max_thesis_loss" → stop
           If daily_pnl / starting_equity <= -max_daily_loss_pct_of_equity:
               Block all new orders for this cycle (non-terminal; continue to log)

Step 7  — Check trailing stop trigger (terminal if exit):
           If current_position_shares > 0
              AND adj_close <= trailing_stop_price:
               EXIT ALL → state = EXITED → stop

Step 8  — Check reduce rules (Section 13), most aggressive applicable rule per tier:
           May fire even if TP also fires on the same bar.
           Does NOT prevent TP from also firing on the same bar.

Step 9  — Check event-risk de-risking orders (Section 15, T-5/T-2/T-1 reductions):
           Only executes if steps 6–7 did not produce an exit.
           If event de-risk fires → execute reduce → skip steps 10–12.

Step 10 — Check take-profit rules (Section 14):
           Only if steps 6–9 produced no exit and no event de-risk action.
           If TP fires → execute → skip steps 11–12.

Step 11 — Check entry rules (Section 9):
           Only if state in {FLAT, STARTER_LONG} AND no action in steps 6–10.

Step 12 — Check add rules (Section 12):
           Only if state in {BASE_LONG, PYRAMID_LONG}
                AND protect_profit_mode = False
                AND no action in steps 6–11.

Step 13 — Log: action taken OR no_action, with full reason and blocker list.
           Write state.json regardless of whether action was taken.
```

**Priority rule (absolute):**
> Steps 6–9 (stops + event de-risk) always override steps 10–12 (TP, entry, add).
> If a hard stop fires on the same bar as a T-2 event de-risk, the hard stop wins.
> The bot exits entirely; event de-risking is NOT executed.

### 8.2 Intraday Check Scope

Intraday checks (at `intraday_check_interval_minutes` frequency) run **Steps 1–6 only**:

```
Step 1: Validate latest price quote (not full bar — just current price feed)
Step 2: Skip (no full bar indicators available intraday)
Step 3: Skip (position unchanged intraday in paper mode)
Step 4: Skip (trailing stop updated at EOD from daily HIGH only)
Step 5: Check event-risk mode entry only (not exit — exit handled at EOD)
Step 6: Check hard stop and max intraday drawdown ONLY
        If intraday_drawdown / starting_equity >= max_intraday_drawdown_pct_of_equity:
            EXIT ALL → state = HALTED, halt_reason = "max_intraday_drawdown"
```

Intraday checks **cannot** trigger: entry, add, reduce, TP, or trailing stop updates.

### 8.3 Reduce Cascade Deduplication

Multiple reduce conditions may be simultaneously true. Execute the most aggressive
applicable action **once per tier**:

```
Add-on reduce tier:
  If Rule B (close < MA10) OR Rule C (drawdown >= threshold) is met:
      execute "reduce ALL add-ons" once — not Rule A
  If ONLY Rule A (close < MA5) is met:
      execute "reduce latest add-on lot" once
  Never execute both on the same bar.

Base reduce tier:
  If Rule F (close < MA50) is met:
      execute "exit all" — overrides Rules D and E
  If Rule D (close < MA20) OR Rule E (drawdown >= threshold) is met:
      execute "reduce base by 50% once"
  Never double-reduce base on the same bar.

Cross-tier: add-on reduce tier fires before base reduce tier on the same bar.
Both tiers can fire on the same bar if conditions warrant.
```

### 8.4 Multiple TP Levels Triggered Simultaneously

On gap-up bars where price crosses multiple layered TP thresholds at once:

```
1. Find the highest triggered level (e.g., if price is at +55%: highest = 50% tier)
2. Mark ALL lower triggered levels as triggered (30% also marked)
3. Execute ONLY the highest applicable level's reduction
4. Compute reduction using position size at START of the decision cycle
```

---

## 9. Entry Logic

### 9.1 Starter Entry

**Conditions (all must be true):**
```
thesis.enabled = true
state = FLAT
adj_close > ma20
distance_from_ma10 <= max_distance_above_ma10_pct        (close not too extended)
distance_from_ma20 <= max_distance_above_ma20_pct
intraday_return <= max_intraday_gain_pct
gap_up_pct <= max_gap_up_pct
volume >= min_volume_vs_20d_avg * avg_volume_20d
trading_days_to_event > stop_new_entry_trading_days_before
```

**Action:**
```
Buy: floor(exposure_pct * available_capital / entry_price) shares
state → STARTER_LONG
Set initial_stop_price = calc_initial_stop(fill_price, atr14)
Set last_add_price = fill_price  [starter is not an "add"; sets baseline]
```

### 9.2 Pullback Base Entry

**Conditions (all must be true):**
```
state in {FLAT, STARTER_LONG}
adj_close > ma20
ma20 > ma50
drawdown_from_20d_high >= min_drawdown_from_20d_high_pct   (3% min pullback)
drawdown_from_20d_high <= max_drawdown_from_20d_high_pct   (8% max pullback)
if require_reclaim_ma5: adj_close > ma5
trading_days_to_event > stop_new_entry_trading_days_before
```

Note: `down_volume` condition is NOT evaluated in v0.1.

**Action:**
```
Buy: floor(additional_exposure_pct * available_capital / entry_price) shares
state → BASE_LONG
Recalculate avg_entry_price from all lots
Set last_add_price = avg_entry_price   [initializes add trigger reference]
```

### 9.3 Breakout Base Entry

**Conditions (all must be true):**
```
state in {FLAT, STARTER_LONG}
adj_close > prior_highest_high_20d     [uses SHIFTED 20d high — see Section 6.1]
volume > min_volume_vs_20d_avg * avg_volume_20d    (1.5x)
adj_close > ma10 AND ma10 > ma20 AND ma20 > ma50   (stacked MAs)
distance_from_ma10 <= max_distance_above_ma10_pct
intraday_return <= max_intraday_gain_pct
trading_days_to_event > stop_new_entry_trading_days_before
```

**Action:**
```
Buy: floor(additional_exposure_pct * available_capital / entry_price) shares
state → BASE_LONG
Recalculate avg_entry_price from all lots
Set last_add_price = avg_entry_price   [initializes add trigger reference]
```

---

## 10. Position Sizing

```
available_capital = starting_equity * (1 - reserve_cash_pct)
```

### 10.1 Initial Entry Sizing (Starter or Base)

Uses the full thesis risk budget:

```
max_total_risk_budget = starting_equity * max_total_capital_at_risk_pct
entry_price           = reference_price (next-bar open estimate for paper)
stop_price            = calc_initial_stop(entry_price, atr14)    [Section 11]
risk_per_share        = entry_price - stop_price

shares_by_risk     = floor(max_total_risk_budget / risk_per_share)
target_notional    = intended_exposure_pct * available_capital
shares_by_exposure = floor(target_notional / entry_price)
final_shares       = min(shares_by_risk, shares_by_exposure)
```

If `final_shares <= 0`: no trade, log blocker `position_size_zero`.

### 10.2 Add-On Sizing (Pyramiding Adds)

Uses REMAINING thesis risk budget to enforce the thesis-level risk cap:

```
max_total_risk_budget = starting_equity * max_total_capital_at_risk_pct

current_open_risk = sum(
    (lot.entry_price - trailing_stop_price) * lot.shares
    for lot in [base_lot] + addon_lots
)
# trailing_stop_price is already updated (Step 4 fires before Step 12)

remaining_risk_budget      = max(0.0, max_total_risk_budget - current_open_risk)
add_price                  = reference_price (next-bar open estimate)
incremental_risk_per_share = add_price - trailing_stop_price

if incremental_risk_per_share <= 0:
    Block add. Log blocker: stop_above_entry_price.

shares_by_risk     = floor(remaining_risk_budget / incremental_risk_per_share)
target_notional    = add_sizes_pct[add_count] * available_capital
shares_by_exposure = floor(target_notional / add_price)
final_shares       = min(shares_by_risk, shares_by_exposure)
```

If `remaining_risk_budget <= 0`: block add, log blocker `thesis_risk_budget_exhausted`.
If `final_shares <= 0`: block add, log blocker `position_size_zero`.

The add_sizes_pct array (0-indexed by add_count):
```
add_count=0 (first add) → add_sizes_pct[0] = 8% of available_capital
add_count=1             → add_sizes_pct[1] = 6%
add_count=2             → add_sizes_pct[2] = 4%
add_count=3             → add_sizes_pct[3] = 2%
```

---

## 11. Initial Stop Logic

```
pct_stop  = entry_price * (1 - stop_pct)
atr_stop  = entry_price - atr_multiplier * atr14
initial_stop = min(pct_stop, atr_stop)   # lower = wider stop for long
```

Example (entry=900, stop_pct=12%, ATR14=50, atr_multiplier=2.5):
```
pct_stop  = 792
atr_stop  = 775
initial_stop = 775
```

---

## 12. Add Logic

### 12.1 Conditions (ALL must be true)

```
state in {BASE_LONG, PYRAMID_LONG}
add.enabled = true
add_count < max_add_count
protect_profit_mode = False
days_since_entry >= min_days_after_entry_before_first_add
days_since_last_add >= min_days_between_adds
unrealized_profit_pct >= min_unrealized_profit_before_first_add_pct  [>= 0 implied]
adj_close >= last_add_price * (1 + add_trigger_pct)
adj_close > ma10
ma10 > ma20
intraday_return <= forbid_add_if_intraday_gain_above_pct
distance_from_ma10 <= forbid_add_if_distance_above_ma10_pct
distance_from_ma20 <= forbid_add_if_distance_above_ma20_pct
trading_days_to_event > stop_adding_if_event_within_trading_days
No stop, reduce, event de-risk, or TP action was triggered this cycle (Steps 6–10)
```

### 12.2 last_add_price Initialization

```
When add_count = 0 (first add attempt):
    last_add_price = avg_entry_price  [set at BASE_LONG entry — see Sections 9.2, 9.3]

When add_count > 0:
    last_add_price = fill_price of most recent add lot
```

### 12.3 Add Execution

```
Compute final_shares using Section 10.2 (remaining risk budget sizing)
If final_shares <= 0: block, log blocker

Buy add-on shares
Append to addon_lots: LotRecord(lot_id=f"add_{add_count+1}", entry_price=fill_price, ...)
Increment add_count
Update last_add_price = fill_price
Recalculate avg_entry_price from all lots (Section 7.2)
state → PYRAMID_LONG (if not already)

If add_count now equals max_add_count:
    protect_profit_mode = True
```

---

## 13. Reduce Logic

### 13.1 LIFO Add-On Reduce Algorithm

All reduce operations work in **whole shares** (floor when computing target count).

```python
def lifo_reduce(addon_lots: list, shares_to_sell: int) -> tuple[list, int]:
    """
    Reduce add-on lots LIFO (latest lot first).
    Returns: (updated_lots, actual_shares_sold)
    """
    remaining = shares_to_sell
    lots = list(reversed(addon_lots))   # latest first
    updated = []
    for lot in lots:
        if remaining <= 0:
            updated.insert(0, lot)
            continue
        sell = min(lot.shares, remaining)
        remaining -= sell
        if lot.shares - sell > 0:
            updated.insert(0, LotRecord(lot.lot_id, lot.entry_price,
                                        lot.shares - sell, lot.entry_date))
    # rebuild: lots not yet processed are already in correct order
    return updated, shares_to_sell - remaining
```

After any LIFO reduce: recalculate `avg_entry_price`, update `current_position_shares`.

### 13.2 Add-On Reduce Rules

```
[Rule A] if adj_close < ma5:
    target = reduce latest addon lot (entire lot, LIFO = tail of addon_lots)

[Rule B] if adj_close < ma10:
    target = reduce ALL addon lots

[Rule C] if drawdown_from_peak >= reduce_all_addons_if_drawdown_from_peak_pct (10%):
    target = reduce ALL addon lots

Cascade deduplication (Section 8.3):
    If Rule B OR Rule C → execute "reduce all addons" once
    If ONLY Rule A → execute "reduce latest addon lot" once
    Never run both on the same bar
```

### 13.3 Base Reduce Rules

```
[Rule D] if adj_close < ma20:
    target = reduce base_lot.shares by 50% (floor)

[Rule E] if drawdown_from_peak >= reduce_base_half_if_drawdown_from_peak_pct (15%):
    target = reduce base_lot.shares by 50% (floor)

[Rule F] if adj_close < ma50:
    target = exit ALL (base_lot + remaining addon_lots)
    state → EXITED

Cascade deduplication:
    If Rule F → exit all (overrides D and E)
    If Rule D OR Rule E → reduce base by 50% once
    Never double-reduce base on the same bar
```

After base reduce to zero → state = EXITED.

### 13.4 drawdown_from_peak Formula

```
drawdown_from_peak = (highest_price_since_entry - adj_close) / highest_price_since_entry
```

- `highest_price_since_entry` updated from daily HIGH (Step 4 of decision engine)
- Comparison value is `adj_close` (not intraday low)

---

## 14. Take Profit Logic

### 14.1 Layered Profit TP

```
profit_from_avg_entry = (adj_close - avg_entry_price) / avg_entry_price
```

Check levels highest-first. If multiple newly triggered, execute only the highest
(Section 8.4); mark all triggered levels.

**Reduction:** uses LIFO algorithm (Section 13.1), add-ons exhausted before base.

```
target_shares_to_sell = floor(reduce_pct_of_total_position * current_position_shares)
Execute LIFO sell across addon_lots then base_lot if needed
```

**Trailing stop ratchet after each layered TP:**

```
move_stop_to = breakeven:         trailing_stop_price = max(trailing_stop_price, avg_entry_price)
move_stop_to = lock_20pct_profit: trailing_stop_price = max(trailing_stop_price, avg_entry_price * 1.20)
move_stop_to = lock_35pct_profit: trailing_stop_price = max(trailing_stop_price, avg_entry_price * 1.35)
move_stop_to = lock_50pct_profit: trailing_stop_price = max(trailing_stop_price, avg_entry_price * 1.50)
```

Trailing stop is **only ever raised, never lowered**.

### 14.2 Target Price TP

Fires from any state with an open position:

```
if adj_close >= thesis.target_price
   AND target_price_tp_triggered = False
   AND state in {STARTER_LONG, BASE_LONG, PYRAMID_LONG, REDUCE_MODE, EVENT_RISK_MODE}:

    target_shares = floor(reduce_pct_of_remaining_position * current_position_shares)
    Execute LIFO sell
    target_price_tp_triggered = True

    if runner_mode.enabled:
        runner_target_shares = floor(base_lot.shares * runner_position_pct_of_original_base)
        # base_lot.shares AT THIS MOMENT (may have been partially reduced already)
        if current_position_shares > runner_target_shares:
            shares_to_sell = current_position_shares - runner_target_shares
            Execute LIFO sell to reach runner_target_shares
        state.runner_target_shares = runner_target_shares
        state.runner_mode_active = True
        state.trailing_stop_price = adj_close * (1 - runner_mode.trailing_stop_pct)
        state → RUNNER_LONG
```

### 14.3 Exposure TP

```
current_exposure_pct = (current_position_shares * adj_close) / starting_equity

if current_exposure_pct >= hard_exposure_cap_pct:
    target_notional  = reduce_to_exposure_pct * starting_equity
    target_shares    = floor(target_notional / adj_close)
    shares_to_sell   = current_position_shares - target_shares
    Execute LIFO sell

if current_exposure_pct >= soft_exposure_cap_pct (and < hard cap):
    Block new adds only (no sell order)
```

### 14.4 Profit Giveback TP

```
unrealized_pnl_pct     = unrealized_pnl / starting_equity
peak_unrealized_pnl_pct = max ever seen (tracked in state.peak_unrealized_pnl_pct)

giveback_pct = (peak_unrealized_pnl_pct - unrealized_pnl_pct) / peak_unrealized_pnl_pct

For each tier (highest min_unrealized_profit_pct_of_equity first):
    if peak_unrealized_pnl_pct >= tier.min_unrealized_profit_pct_of_equity:
        if giveback_pct > tier.max_giveback_pct:
            Reduce add-ons first (LIFO), then base if needed to recover within limit
            break   ← only one tier fires per bar
```

### 14.5 Trailing Take Profit

```
profit_from_avg  = (adj_close - avg_entry_price) / avg_entry_price
drawdown_from_peak = (highest_price_since_entry - adj_close) / highest_price_since_entry

For each tier (highest min_profit_pct_from_avg_entry first):
    if profit_from_avg >= tier.min_profit_pct_from_avg_entry:
        if drawdown_from_peak >= tier.trailing_pct:
            Execute tier.action; break    ← only one tier fires per bar
```

Actions:
```
reduce_addons_50pct: sell floor(total_addon_shares * 0.50) using LIFO
reduce_all_addons:   sell all addon lots using LIFO
reduce_to_core:      sell all addon lots + reduce base_lot to floor(base_lot.shares * 0.25)
```

### 14.6 Runner Mode

Runner mode is entered via target price TP (Section 14.2). Once in RUNNER_LONG:

```
runner_mode_active = True
runner_target_shares already set (stored in state)
trailing_stop_price = adj_close * (1 - runner_mode.trailing_stop_pct)   [reset at entry]
Add orders are blocked (RUNNER_LONG state prevents adds in decision engine)

Trailing stop check in RUNNER_LONG:
    if adj_close <= trailing_stop_price:
        EXIT ALL → state = EXITED
```

---

## 15. Event Risk Logic

Event date T = `thesis.event_date`. Trading days = business days (Mon–Fri, US market).

**Two separate concerns — do not conflate:**

**A — Mode Transition (Step 5 of decision engine, NO orders generated):**
```
When trading_days_to_event enters threshold → save prior_state, state = EVENT_RISK_MODE
When post-event cooldown elapsed → restore prior_state, clear EVENT_RISK_MODE
```

**B — De-Risking Orders (Step 9 of decision engine, after stops):**
```
Only executes if Steps 6–7 did NOT produce an exit.

T-10 trading days:  [Handled in Step 5 — transition only, adds/entries blocked by state]

T-5 trading days:
    If current_exposure_pct > max_exposure_before_event_pct (50%):
        Reduce to 50% using LIFO

T-2 trading days:
    If current_exposure_pct > core_exposure_before_event_pct (20%):
        Reduce to core (20%) using LIFO

T-1 trading day:
    If force_flat_before_event = true:
        EXIT ALL → state = EXITED
```

**Post-event (Step 5 on first bar after cooldown):**
```
restore state = prior_state (saved before EVENT_RISK_MODE entry)
if reset_add_count: add_count = 0
if add_count reset: clear protect_profit_mode = False  [adds re-enabled]
if require_new_trend_confirmation: entry conditions re-evaluated before any new add
```

**Priority rule:**
```
If hard stop (Step 6) or trailing stop (Step 7) fires on the same bar as T-2 de-risk:
    Stop fires. Bot exits entirely. Event de-risking (Step 9) is NOT executed.
```

---

## 16. Stop Logic

### 16.1 Hard Stop

```
if current_position_shares > 0 AND adj_close <= initial_stop_price:
    EXIT ALL (market order allowed)
    state → EXITED
    Update realized_pnl
```

### 16.2 Trailing Stop

Trailing stop is updated at the **START of each EOD decision cycle** (Step 4),
**BEFORE any trigger comparison**. This prevents same-bar miss when price creates
a new high and then closes below the resulting stop.

**Update sequence (also used identically in backtest loop, Section 21.3):**
```
Step 4a: highest_price_since_entry = max(state.highest_price_since_entry, bar.high)

Step 4b: trailing_pct = default_trailing_pct
         profit_from_avg = (adj_close - avg_entry_price) / avg_entry_price
         for tier in profit_tiers (highest min_profit_pct first):
             if profit_from_avg >= tier.min_profit_pct:
                 trailing_pct = tier.trailing_pct; break

Step 4c–d: trailing_stop_price = max(state.trailing_stop_price,
                                      highest_price_since_entry * (1 - trailing_pct))
           [Trailing stop is NEVER lowered]

Step 4e: Persist to state BEFORE proceeding with trigger checks.
```

**Trigger check (Step 7, uses adj_close — NOT intraday low):**
```
if adj_close <= trailing_stop_price:
    EXIT ALL (market order allowed)
    state → EXITED
```

### 16.3 Max Loss

```
if thesis_pnl / starting_equity <= -max_thesis_loss_pct_of_equity:
    EXIT ALL → state = HALTED, halt_reason = "max_thesis_loss"
    Manual reset_state.py --confirm required

if daily_pnl / starting_equity <= -max_daily_loss_pct_of_equity:
    Block all new orders for remainder of day (non-terminal — position held)
    Log: daily_loss_limit_hit

if intraday_drawdown / starting_equity >= max_intraday_drawdown_pct_of_equity:
    [Intraday monitor check only — Section 8.2]
    EXIT ALL → state = HALTED, halt_reason = "max_intraday_drawdown"
```

---

## 17. Leverage Logic

v0.1: `leverage.enabled = false`. All sizing uses `max_leverage = 1.0`.

Future only (do not implement in v0.1):
```
Leverage allowed only with profit cushion >= profit_cushion_required_pct.
Max leverage = max_leverage_with_profit_cushion (1.5x).
If trading_days_to_event <= 5: max_leverage = 1.0.
```

---

## 18. Execution Logic

### 18.1 Paper Broker Fill Model

**Architecture: paper_broker is stateless.**

```python
def execute(decision: Decision, fill_ref_price: float, state: ThesisState) -> Fill:
    """
    Pure function. Does NOT maintain internal position state.
    Does NOT read or write state.json.

    fill_ref_price = next trading day's open price (from bar T+1 in backtest,
                     or live quote for paper-live mode).

    Returns a Fill object. Caller applies Fill to ThesisState via state_writer.apply_fill().
    """
    slippage = random.uniform(0, max_slippage_bps / 10000)
    if decision.side == "buy":
        fill_price = fill_ref_price * (1 + slippage)
    else:
        fill_price = fill_ref_price * (1 - slippage)

    # compute realized_pnl for sell orders using lot cost basis from state
    ...
    return Fill(shares=decision.shares, fill_price=fill_price,
                realized_pnl=realized_pnl, commission=0.0)
```

`order_timeout_seconds` and `retry_count` are ignored in paper/backtest mode.

### 18.2 Order Side by Action Type

```
BUY_STARTER, BUY_BASE, BUY_ADDON    → side = buy,  fill = next_bar_open * (1 + slippage)
SELL_REDUCE_ADDON, SELL_REDUCE_BASE → side = sell, fill = next_bar_open * (1 - slippage)
SELL_TAKE_PROFIT                    → side = sell, fill = next_bar_open * (1 - slippage)
SELL_STOP, SELL_TRAILING_STOP       → side = sell, fill = next_bar_open * (1 - slippage)
SELL_EVENT_DERISKING                → side = sell, fill = next_bar_open * (1 - slippage)
EXIT_ALL                            → side = sell, fill = next_bar_open * (1 - slippage)
```

Commission: `0.0` in v0.1 (placeholder field in Fill model for future use).

---

## 19. Logging

### 19.1 orders.csv
```csv
timestamp,symbol,action,side,shares,order_type,limit_price,fill_price,slippage_bps,status,reason,state_before,state_after
```

### 19.2 positions.csv
```csv
timestamp,symbol,state,shares,avg_entry_price,last_price,notional,exposure_pct,unrealized_pnl,realized_pnl,highest_price,trailing_stop
```

### 19.3 risk.csv
```csv
timestamp,symbol,state,equity,exposure_pct,leverage,drawdown_from_peak,unrealized_pnl_pct,peak_unrealized_pnl_pct,thesis_pnl,daily_pnl,risk_status,blockers
```

### 19.4 decisions.csv
```csv
timestamp,symbol,state,decision,reason,blockers,indicators_snapshot,config_snapshot_hash
```

`config_snapshot_hash` = SHA256 of serialized config (deterministic sorted-key JSON). Computed once at startup.

Every no-action cycle must log a `decisions.csv` row with `decision = no_action`.

---

## 20. Daily Summary Output

```
MU Long Thesis Bot — Daily Summary
====================================
State:               PYRAMID_LONG
Protect Profit Mode: False
Close:               1000.00
Position:            36 shares  (base: 22 | add-ons: 14)
Avg Entry:           924.06
Exposure:            36.0%
Unrealized PnL:      +7.5% (account)
Add Count:           2 / 4
Highest (daily HIGH): 1100.00
Trailing Stop:       990.00
Initial Stop:        775.00
Days to Event:       15
Runner Mode:         No

--- Today's Action ---
Action:              NO ACTION
Reason:              Close too extended above MA10 (11.2% vs max 12.0%)
Blockers:            distance_from_ma10_exceeded

--- Risk Budget ---
Max thesis risk:     $6,000
Open risk (all lots): $4,842
Remaining budget:    $1,158

--- Next Bar ---
Next Possible:       Add if price consolidates below MA10 extension threshold
```

---

## 21. Backtest Requirements

### 21.1 Backtest Universe
```
MU, NVDA, SMCI, MSTR, TSLA, AMD, AVGO, COIN
```

### 21.2 Backtest Regimes
```
Strong uptrend / Choppy uptrend / False breakout
Earnings gap (both gap-up and gap-down) / Major drawdown / Post-parabolic reversal
```

### 21.3 Backtest Loop Design

```python
# Correct EOD sequence — mirrors live decision engine exactly
for i, bar_T in enumerate(bars):
    if i + 1 >= len(bars):
        break   # no next bar to fill against

    bar_T_plus_1 = bars[i + 1]

    # Step 1: trailing stop update BEFORE decision (same as live Step 4)
    if state.current_position_shares > 0:
        state.highest_price_since_entry = max(
            state.highest_price_since_entry or 0.0, bar_T.high
        )
        trailing_pct = determine_trailing_pct(state, config)
        candidate = state.highest_price_since_entry * (1 - trailing_pct)
        state.trailing_stop_price = max(state.trailing_stop_price or 0.0, candidate)

    # Step 2: full decision using bar T (no look-ahead)
    indicators = calc_indicators(df.iloc[:i+1], config)   # history up to bar T only
    decision = decision_engine.run(state, indicators, config)

    # Step 3: fill at bar T+1 open
    fill = None
    if decision.action != ActionType.NO_ACTION:
        fill = paper_broker.execute(decision, bar_T_plus_1.open, state)
        state = state_writer.apply_fill(state, fill)

    # Step 4: log
    log_all(bar_T, decision, fill, state)
```

**Look-ahead safeguard:**
`calc_indicators(df.iloc[:i+1], ...)` — the slice includes bar T but NOT bar T+1.
`prior_highest_high_20d` is computed on this slice and is inherently shifted.

**Gap risk:**
If `bar_T_plus_1.open` < `trailing_stop_price`, fill executes at `bar_T_plus_1.open`
(no guaranteed stop). This simulates real gap risk.

### 21.4 Backtest Metrics (19 required)
```
total_return, buy_and_hold_return, excess_return_vs_buy_hold,
max_drawdown, max_intraday_drawdown,
number_of_entries, number_of_adds, number_of_reduces,
number_of_take_profits, number_of_stops,
win_rate, average_win, average_loss, largest_loss, profit_factor,
time_in_market, average_exposure, max_exposure,
event_window_pnl, gap_loss_count
```

### 21.5 Charts (Phase 4)
```
Price chart with entry/add/reduce/TP/stop markers
Equity curve vs buy-and-hold
Drawdown curve
Exposure over time
Position size over time
```

---

## 22. Unit Tests

### 22.1 Config Tests
```
test_config_loads_valid_yaml
test_config_rejects_live_mode_in_v01
test_config_requires_event_date_when_event_risk_enabled
test_config_rejects_mismatched_add_sizes_count
test_config_snapshot_hash_is_deterministic
test_config_rejects_duplicate_event_date_in_event_risk_block
```

### 22.2 Indicator Tests
```
test_moving_averages_correct_on_synthetic_data
test_atr_correct_on_synthetic_data
test_prior_highest_high_20d_is_shifted_by_one_bar
test_drawdown_from_20d_high_uses_shifted_prior_high
test_gap_up_pct_uses_raw_open_not_adj_close
test_intraday_return_uses_raw_open
test_indicators_return_nan_when_insufficient_history
```

### 22.3 State Machine Tests
```
test_flat_to_starter_long
test_flat_to_base_long_direct
test_starter_to_base_long
test_base_to_pyramid_long
test_pyramid_to_pyramid_on_add
test_pyramid_to_reduce_mode
test_reduce_mode_to_base_long_after_all_addons_removed
test_reduce_mode_to_exited_when_base_fully_exited
test_any_long_to_event_risk_mode
test_event_risk_mode_restores_prior_state_after_cooldown
test_any_long_to_runner_long_on_target_tp
test_any_long_to_exited_on_hard_stop
test_runner_long_to_exited_on_runner_stop
test_any_state_to_halted_on_max_loss
test_halted_requires_manual_reset
test_exited_requires_manual_reset
test_invalid_transition_raises_error
test_protect_profit_mode_set_at_max_add_count
test_protect_profit_mode_set_at_30pct_unrealized_profit
test_add_blocked_when_protect_profit_mode_true
test_protect_profit_mode_cleared_on_event_reset
test_state_remains_pyramid_long_when_protect_profit_mode_set
test_reduce_fires_normally_when_protect_profit_mode_true
```

### 22.4 Entry Tests
```
test_no_entry_when_thesis_disabled
test_no_entry_when_close_below_ma20
test_no_entry_when_intraday_gain_too_large
test_no_entry_when_gap_up_too_large
test_starter_entry_when_all_conditions_met
test_pullback_base_entry_from_flat
test_pullback_base_entry_from_starter_long
test_breakout_base_uses_prior_highest_high_not_current
test_breakout_base_entry_when_conditions_met
test_no_entry_inside_event_window
```

### 22.5 Add Tests
```
test_no_add_when_position_losing
test_no_add_before_cooldown_days
test_no_add_when_profit_below_threshold
test_no_add_when_close_below_ma10
test_no_add_when_ma10_below_ma20
test_no_add_when_add_count_maxed
test_no_add_inside_event_window
test_no_add_when_protect_profit_mode_true
test_first_add_trigger_uses_avg_entry_as_last_add_price
test_add_when_all_conditions_met
test_add_blocked_when_remaining_risk_budget_exhausted
test_add_blocked_when_tp_fired_same_cycle
```

### 22.6 Reduce Tests
```
test_reduce_latest_addon_when_only_close_below_ma5
test_reduce_all_addons_when_close_below_ma10
test_reduce_all_addons_not_doubled_when_ma5_and_ma10_both_broken
test_reduce_all_addons_when_drawdown_from_peak_exceeds_threshold
test_reduce_base_half_when_close_below_ma20
test_base_not_double_reduced_when_ma20_and_drawdown_both_trigger
test_exit_all_when_close_below_ma50
test_lifo_partial_reduce_exhausts_latest_lot_first
test_lifo_reduce_splits_partially_exhausted_lot
test_avg_entry_recalculated_correctly_after_reduce
test_reduce_mode_to_exited_when_base_lot_shares_reach_zero
```

### 22.7 Take Profit Tests
```
test_layered_tp_triggers_at_correct_profit_level
test_layered_tp_triggers_only_once_per_level
test_only_highest_tp_level_fires_on_gap_up_bar
test_all_lower_tp_levels_marked_triggered_on_gap_up
test_layered_tp_reduces_addons_first_lifo
test_trailing_stop_raised_after_layered_tp
test_target_price_tp_fires_from_base_long_state
test_target_price_tp_fires_from_event_risk_mode
test_target_price_tp_uses_target_price_tp_triggered_field
test_target_price_tp_enters_runner_mode
test_exposure_tp_triggers_at_hard_cap
test_exposure_tp_soft_cap_blocks_add_only
test_profit_giveback_tp_triggers_when_giveback_exceeds_threshold
test_trailing_tp_fires_at_correct_profit_and_drawdown_tier
test_runner_mode_uses_separate_tight_trailing_stop
```

### 22.8 Stop Tests
```
test_initial_stop_uses_wider_of_pct_or_atr_stop
test_hard_stop_exits_all_and_sets_exited
test_trailing_stop_updated_from_daily_high_before_trigger_check
test_trailing_stop_never_lowered
test_trailing_stop_trigger_uses_adj_close_not_intraday
test_trailing_stop_correct_when_bar_makes_new_high_then_closes_below
test_max_thesis_loss_exits_and_halts_bot
test_max_daily_loss_blocks_orders_only
test_intraday_drawdown_halts_bot
```

### 22.9 Event Risk Tests
```
test_event_t10_transitions_to_event_risk_mode_only
test_event_t10_does_not_generate_orders
test_event_t5_de_risking_generates_sell_order
test_event_t2_reduces_to_core_20pct
test_event_t1_force_flat_if_configured
test_stop_overrides_event_derisking_on_same_bar
test_post_event_cooldown_restores_prior_state
test_post_event_add_count_reset
test_protect_profit_mode_cleared_after_event_reset
```

---

## 23. Overall Acceptance Criteria

v0.1 is complete when:
```
 1. Config loads from YAML and all 9 validation rules are enforced.
 2. All unit tests pass: pytest tests/ -q (zero failures, zero errors).
 3. Historical OHLCV loads for MU via yfinance.
 4. Indicators calculate correctly: prior_highest_high_20d is verified shifted.
 5. State machine covers all 9 states with tested valid and invalid transitions.
 6. Decision engine returns correct Decision for all synthetic scenarios.
 7. Paper broker fills at next-bar open ± slippage (stateless, state.json is truth).
 8. Single EOD decision cycle writes state.json and all 4 CSVs.
 9. Daily summary generated to stdout and file.
10. Backtest runs end-to-end for MU with correct loop order (trailing stop updated first).
11. All 19 backtest metrics produced.
12. No live trading mode enabled anywhere in codebase.
13. reset_state.py requires --confirm flag; fails without it.
14. Every no-action cycle logged to decisions.csv with blockers.
```

---

## 24. CLI Commands

```bash
python scripts/run_backtest.py --config config/mu_long_thesis.yaml --ticker MU
python scripts/run_paper.py --config config/mu_long_thesis.yaml
python scripts/run_daily_decision.py --config config/mu_long_thesis.yaml
python scripts/generate_daily_summary.py --config config/mu_long_thesis.yaml
python scripts/reset_state.py --config config/mu_long_thesis.yaml --confirm
pytest tests/ -q
```

---

## 25. Development Phases

### Phase 1 — Core Models, Config, State Machine, Indicators [START HERE]

**Files to create:**
```
src/core/models.py           All Pydantic/dataclass models (see Section 29 for schemas)
src/core/config_loader.py    load_config(path: str) → BotConfig
                             validate_config(cfg: BotConfig) → None (raises on violation)
src/core/state_machine.py    BotState enum (9 values), StateMachine class
src/data/indicators.py       calc_indicators(df: pd.DataFrame, cfg: BotConfig) → IndicatorSnapshot
config/mu_long_thesis.yaml   Canonical sample config (copy from Section 5)
tests/test_config.py         6 tests from Section 22.1
tests/test_state_machine.py  23 tests from Section 22.3
tests/test_indicators.py     7 tests from Section 22.2
```

**Phase 1 acceptance gate:**
```
1. Config loads and all 9 validation rules enforced (ValueError on violation)
2. BotState enum has exactly 9 values — PROTECT_PROFIT is not one of them
3. VALID_TRANSITIONS dict matches Section 4.2 exactly
4. StateMachine.transition() raises ValueError on any invalid transition
5. ThesisState model has base_lot, addon_lots, protect_profit_mode,
   target_price_tp_triggered, runner_target_shares, prior_state fields
6. prior_highest_high_20d in IndicatorSnapshot equals rolling max(high,20).shift(1)
7. pytest tests/test_config.py tests/test_state_machine.py tests/test_indicators.py -q
   → 0 failures, 0 errors
```

**DO NOT proceed to Phase 2 until the Phase 1 gate passes.**

### Phase 2 — Decision Logic (pure functions, no I/O)

```
src/strategy/entry.py, add.py, reduce.py, take_profit.py,
             stops.py, event_risk.py, sizing.py
src/core/decision_engine.py
tests/test_entry_logic.py, test_add_logic.py, test_reduce_logic.py,
      test_take_profit.py, test_stops.py, test_event_risk.py,
      test_sizing.py, test_decision_engine.py
```

Gate: All strategy tests pass. No I/O in `src/strategy/` — pure functions only.

### Phase 3 — Paper Broker, State Writer, Logging

```
src/execution/paper_broker.py     execute(decision, fill_ref_price, state) → Fill
src/execution/order_manager.py
src/reporting/logger.py
src/reporting/state_writer.py
src/reporting/daily_summary.py
scripts/run_daily_decision.py
scripts/generate_daily_summary.py
scripts/reset_state.py
tests/test_paper_broker.py
```

Gate: Single daily decision cycle writes correct state.json and all 4 CSVs.

### Phase 4 — Backtester

```
src/data/market_data.py
src/backtest/backtester.py
src/backtest/metrics.py
src/backtest/charts.py
scripts/run_backtest.py
tests/test_backtester.py
```

Gate: MU backtest runs end-to-end; 19 metrics produced; no look-ahead bias.

### Phase 5 — Hardening

```
src/risk/validators.py
src/risk/risk_manager.py
```

Gate: Bot refuses on bad data; halt requires explicit reset; state mismatch blocks orders.

### Phase 6 — Live Readiness

Blocked until Phase 5 acceptance. Not in v0.1 scope.

---

## 26. Non-Negotiable Safety Rules

```
 1. Never add to a losing position (unrealized_pnl <= 0).
 2. Never add inside event-risk no-add window.
 3. Never trade with stale or invalid data (NaN indicators, missing adj_close).
 4. Never trade when state.json internal consistency check fails.
 5. Never exceed max_symbol_exposure_pct.
 6. Never exceed max_thesis_loss (halt and exit instead).
 7. Never ignore hard stop because target price is not yet reached.
 8. Never re-enter after HALTED without explicit manual reset.
 9. Never enable live trading by default (mode: paper in all configs).
10. Trailing stop can only move higher — never lower.
11. TP / reduce / stop actions always block add on the same bar.
12. protect_profit_mode cannot be set by config — only by runtime conditions.
```

---

## 27. One-Sentence Product Definition

This is a **long-only thesis position manager** that starts small, adds only when price
confirms the thesis, takes profit using LIFO add-on reduction, protects gains with
trailing and giveback rules, de-risks before earnings, and halts when risk limits are
breached.

---

## 28. Dependencies

```
python        >= 3.11
pydantic      >= 2.0
yfinance      >= 0.2.40
pandas        >= 2.0
numpy         >= 1.26
pytest        >= 7.0
pyyaml        >= 6.0
matplotlib    >= 3.8      (Phase 4 charts only)
```

No other dependencies. Do not add libraries without explicit justification.

---

## 29. Phase 1 Model Schemas (Implementation Reference)

These schemas are the target for `src/core/models.py`. Use Pydantic v2.

```python
from __future__ import annotations
from datetime import date, datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, field_validator, model_validator


# ── State Machine ───────────────────────────────────────────────────────────

class BotState(str, Enum):
    FLAT            = "FLAT"
    STARTER_LONG    = "STARTER_LONG"
    BASE_LONG       = "BASE_LONG"
    PYRAMID_LONG    = "PYRAMID_LONG"
    REDUCE_MODE     = "REDUCE_MODE"
    EVENT_RISK_MODE = "EVENT_RISK_MODE"
    RUNNER_LONG     = "RUNNER_LONG"
    EXITED          = "EXITED"
    HALTED          = "HALTED"


# ── Core data models ────────────────────────────────────────────────────────

class LotRecord(BaseModel):
    """Represents one position lot (base or add-on). Same schema for both."""
    lot_id:     str
    entry_price: float
    shares:     int
    entry_date: date


class IndicatorSnapshot(BaseModel):
    """All indicator values for one bar. Passed to strategy functions."""
    date:                   date
    adj_close:              float
    open:                   float       # raw open
    high:                   float       # raw high
    low:                    float       # raw low
    volume:                 float
    prev_adj_close:         float
    ma5:                    float
    ma10:                   float
    ma20:                   float
    ma50:                   float
    atr14:                  float
    avg_volume_20d:         float
    prior_highest_high_20d: float       # rolling max(high,20).shift(1)
    drawdown_from_20d_high: float       # uses prior_highest_high_20d
    distance_from_ma10:     float       # (adj_close - ma10) / ma10
    distance_from_ma20:     float       # (adj_close - ma20) / ma20
    intraday_return:        float       # (adj_close - raw_open) / raw_open
    gap_up_pct:             float       # max(0, (raw_open - prev_adj_close) / prev_adj_close)
    gap_down_pct:           float       # max(0, (prev_adj_close - raw_open) / prev_adj_close)


class ThesisState(BaseModel):
    """Full runtime state. Persisted to state.json after every decision cycle."""
    symbol:              str
    state:               BotState     = BotState.FLAT
    prior_state:         Optional[BotState] = None    # saved on EVENT_RISK_MODE entry
    protect_profit_mode: bool         = False
    runner_mode_active:  bool         = False
    runner_target_shares: Optional[int] = None

    thesis_enabled:      bool         = True
    entry_date:          Optional[date] = None

    base_lot:            Optional[LotRecord] = None
    addon_lots:          List[LotRecord]     = []

    avg_entry_price:     Optional[float] = None   # cached; recalculated after every fill
    current_position_shares: int         = 0
    current_notional:    float           = 0.0

    add_count:           int         = 0
    last_add_price:      Optional[float] = None

    highest_price_since_entry: Optional[float] = None   # updated from daily HIGH
    peak_unrealized_pnl_pct:   float            = 0.0
    initial_stop_price:  Optional[float] = None
    trailing_stop_price: Optional[float] = None

    tp_levels_triggered:      List[bool] = [False, False, False, False]
    target_price_tp_triggered: bool      = False

    realized_pnl:  float = 0.0
    unrealized_pnl: float = 0.0
    thesis_pnl:    float = 0.0
    days_to_event: Optional[int] = None

    halted:        bool            = False
    halt_reason:   Optional[str]   = None
    last_action:   Optional[str]   = None
    last_updated:  Optional[datetime] = None


# ── Decision ─────────────────────────────────────────────────────────────────

class ActionType(str, Enum):
    NO_ACTION            = "no_action"
    BUY_STARTER          = "buy_starter"
    BUY_BASE             = "buy_base"
    BUY_ADDON            = "buy_addon"
    SELL_REDUCE_ADDON    = "sell_reduce_addon"
    SELL_REDUCE_BASE     = "sell_reduce_base"
    SELL_TAKE_PROFIT     = "sell_take_profit"
    SELL_STOP            = "sell_stop"
    SELL_TRAILING_STOP   = "sell_trailing_stop"
    SELL_EVENT_DERISKING = "sell_event_derisking"
    EXIT_ALL             = "exit_all"
    HALT                 = "halt"


class Decision(BaseModel):
    """Output of decision_engine.run(). Consumed by paper_broker.execute()."""
    action:     ActionType
    shares:     int                    = 0
    reason:     str
    blockers:   List[str]              = []
    new_state:  Optional[BotState]     = None
    new_protect_profit_mode: Optional[bool] = None
    indicators: Optional[IndicatorSnapshot] = None


# ── Fill (paper broker output) ───────────────────────────────────────────────

class Fill(BaseModel):
    """Returned by paper_broker.execute(). Applied to ThesisState by state_writer."""
    action:       ActionType
    shares:       int
    fill_price:   float
    slippage_bps: float
    realized_pnl: float     = 0.0
    commission:   float     = 0.0    # always 0.0 in v0.1
    timestamp:    datetime


# ── Config skeleton (full schema validated by config_loader) ─────────────────

class BotConfig(BaseModel):
    """
    Full config model. config_loader.load_config() returns this.
    Use nested models or plain dicts for sub-sections.
    Validation rules from Section 5.1 enforced in validate_config().
    """
    # Top-level structure mirrors the YAML sections in Section 5.
    # Implementation detail: use model_config = {"extra": "forbid"} to catch
    # unknown config keys early.
    class Config:
        extra = "forbid"
```

---

## 30. Spec Lineage Summary

| Version | Changes |
|---------|---------|
| v0.1 | Original spec — strong philosophy, incomplete engineering |
| v0.2 | 14 fixes: state machine exits, LIFO algorithm, formulas, paper fill, config dedup, runner state, data source, simultaneous TP |
| v0.2.1 | 12 patches: REDUCE_MODE→EXITED, prior_highest_high_20d shift, explicit base_lot, target_price_tp_triggered, EOD trailing stop order, ANY_LONG_STATE target TP, event-risk priority separation, remaining risk budget for adds, stateless paper_broker, remove down_volume v0.1, OHLC note, PROTECT_PROFIT→flag |
| **Final** | **This document — fully merged, implementation-ready** |
