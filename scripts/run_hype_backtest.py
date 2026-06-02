from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.backtest.historical_loader import execution_network, historical_data_network, load_historical_candles
from src.backtest.data_quality import (
    DataQualityThresholds,
    audit_ohlcv_quality,
    filter_flagged_candles,
)
from src.backtest.pyramiding_backtester import run_research_backtest
from src.core.config_loader import load_config


def build_run_id(coin: str, interval: str) -> str:
    return f"{coin.lower()}_{interval}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline HYPE pyramiding research backtest")
    parser.add_argument("--config", required=True)
    parser.add_argument("--coin", default="HYPE")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--exclude-flagged-candles", action="store_true")
    parser.add_argument("--min-price-threshold", type=float, default=None)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    exec_network = execution_network(config)
    data_network = historical_data_network(config)
    print("execution_network=" + exec_network)
    print("historical_data_network=" + data_network)
    df = load_historical_candles(
        coin=args.coin,
        interval=args.interval,
        start=args.start,
        end=args.end,
        network=data_network,
    )
    quality_report = audit_ohlcv_quality(
        df,
        interval=args.interval,
        thresholds=DataQualityThresholds(close_below_min_price_threshold=args.min_price_threshold),
    )
    quality_policy = "excluded" if args.exclude_flagged_candles else "included"
    if args.exclude_flagged_candles:
        df = filter_flagged_candles(df, quality_report)
    run_id = build_run_id(args.coin, args.interval)
    run_dir = Path("reports/backtests") / args.coin.lower() / run_id
    result = run_research_backtest(
        df=df,
        config=config,
        coin=args.coin,
        interval=args.interval,
        run_dir=run_dir,
        run_id=run_id,
        slippage_bps=args.slippage_bps,
        data_quality=quality_report.summary(policy=quality_policy),
    )
    print("BACKTEST_COMPLETE")
    print("run_dir=" + str(result.run_dir))
    print("total_return_pct=" + str(result.summary["total_return_pct"]))
    print("max_drawdown_pct=" + str(result.summary["max_drawdown_pct"]))
    print("strategy_scope=" + str(result.summary["strategy_scope"]))
    print("data_quality_warning_count=" + str(result.summary["data_quality_warning_count"]))
    print("flagged_candles_policy=" + str(result.summary["flagged_candles_policy"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
