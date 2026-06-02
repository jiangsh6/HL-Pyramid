# HYPE Backtest Research

This layer is offline research only. It does not use the live broker,
reconciliation path, Telegram delivery, or any order placement code.

## Single Backtest

```bash
python scripts/run_hype_backtest.py \
  --config config/hype_candidate_normal_v2.yaml \
  --coin HYPE \
  --interval 1h \
  --start 2026-01-01 \
  --end 2026-02-01
```

Outputs are written under:

```text
reports/backtests/hype/<run_id>/
```

Files:

```text
orders.csv
positions.csv
decisions.csv
signal_diagnostics.csv
equity_curve.csv
backtest_summary.json
backtest_report.md
```

## Timeframe Comparison

```bash
python scripts/compare_hype_timeframes.py \
  --config config/hype_candidate_normal_v2.yaml \
  --start 2026-01-01 \
  --end 2026-02-01
```

Compares `1h` and `4h` without assuming either is better.

## Parameter Sweep

```bash
python scripts/sweep_hype_parameters.py \
  --base-config config/hype_candidate_normal_v2.yaml \
  --interval 1h \
  --start 2026-01-01 \
  --end 2026-02-01
```

Initial grid:

```text
entry.starter.max_distance_above_ma20_pct: [0.05, 0.08, 0.12]
entry.starter.max_intraday_gain_pct: [0.03, 0.05, 0.08]
entry.starter.min_volume_vs_20d_avg: [0.8, 1.0, 1.2]
add.add_trigger_pct: [0.04, 0.06, 0.08, 0.10]
add.max_add_count: [2, 3, 4]
capital.max_total_capital_at_risk_pct: [0.015, 0.03, 0.05]
```

## Metrics

The summary includes return, buy-and-hold comparison, drawdown, volatility,
trade counts, add counts, stop counts, time in market, win/loss summaries,
pyramiding exposure, risk-budget utilization, blocker frequency, and readiness
score diagnostics.

## No-Lookahead Rules

The research backtester applies these rules:

- Decision for bar `N` uses candles through bar `N` close only.
- Fill happens at bar `N+1` open.
- Slippage is deterministic and configurable.
- NaN indicator rows are logged as `NO_ACTION` and cannot trade.
- Future high/low/close values do not influence the current decision.

## Strategy Scope

The first research implementation intentionally matches current Phase 1 scope:

```text
strategy_scope = starter_add_stop_only
```

Enabled scope:

- starter entry
- add logic
- stop/trailing-stop infrastructure

Disabled in this research pass:

- pullback entry
- breakout entry
- reduce
- take profit
- runner mode
- event risk

## What Not To Trust Yet

- The initial version assumes full fills.
- Fees are not modeled separately.
- Partial fills and order resting are not modeled.
- Hyperliquid funding is not fetched for research runs.
- Walk-forward validation is scaffolded but not fully wired.

## Future TradFi Perp Support

The loader and backtester accept `--coin`, so future Hyperliquid-listed
perpetual tickers can be tested structurally. Do not add TradFi ticker configs
until Hyperliquid support and metadata are confirmed.
