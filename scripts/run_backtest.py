#!/usr/bin/env python
"""
Run a full backtest and print the 19 metrics.

Usage:
  python scripts/run_backtest.py --config config/mu_long_thesis.yaml --ticker MU
  python scripts/run_backtest.py --config config/mu_long_thesis.yaml --ticker MU \\
      --start 2023-01-01 --end 2025-01-01 --charts --run-dir reports/bt_mu
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

# Allow running this script directly without an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config_loader import load_config
from src.data.market_data import fetch_ohlcv
from src.backtest.backtester import run_backtest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run long-thesis backtest.")
    parser.add_argument("--config",  required=True,
                        help="Path to YAML config file.")
    parser.add_argument("--ticker",  default=None,
                        help="Override ticker symbol from config.")
    parser.add_argument("--start",   default=None,
                        help="Start date YYYY-MM-DD (default: 1 year ago).")
    parser.add_argument("--end",     default=None,
                        help="End date YYYY-MM-DD (default: today).")
    parser.add_argument("--charts",  action="store_true",
                        help="Save 5 PNG charts to --run-dir.")
    parser.add_argument("--run-dir", default=None,
                        help="Directory for CSV logs and charts.")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    ticker = args.ticker or cfg.symbol["ticker"]
    required_days = cfg.data.get("required_history_days", 120)

    end_date = args.end or date.today().isoformat()
    if args.start:
        start_date = args.start
    else:
        # Add warm-up buffer so indicators have enough history.
        start_dt = date.fromisoformat(end_date) - timedelta(days=required_days * 2)
        start_date = start_dt.isoformat()

    print(f"Fetching {ticker} from {start_date} to {end_date}…")
    df = fetch_ohlcv(ticker, start_date, end_date)
    print(f"  {len(df)} bars loaded.")

    run_dir = args.run_dir
    result = run_backtest(df, cfg, run_dir=run_dir, ticker=ticker)

    print("\n── Backtest Metrics ─────────────────────────────────────")
    for key, val in result.metrics.items():
        if isinstance(val, float):
            print(f"  {key:<35}  {val:.4f}")
        else:
            print(f"  {key:<35}  {val}")

    if args.charts:
        try:
            from src.backtest.charts import generate_charts
            chart_dir = run_dir or f"reports/backtest_{ticker}"
            generate_charts(result, cfg, chart_dir)
            print(f"\nCharts saved to: {chart_dir}")
        except ImportError as exc:
            print(f"\nCharts skipped: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
