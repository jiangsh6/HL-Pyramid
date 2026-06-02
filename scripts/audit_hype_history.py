from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.backtest.data_quality import (
    DataQualityThresholds,
    audit_ohlcv_quality,
    write_history_quality_report,
)
from src.backtest.historical_loader import load_historical_candles


def build_run_id(coin: str, interval: str) -> str:
    return f"{coin.lower()}_{interval}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit HYPE historical candle quality for research backtests")
    parser.add_argument("--coin", default="HYPE")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--network", default="mainnet")
    parser.add_argument("--extreme-bar-return-abs-pct", type=float, default=30.0)
    parser.add_argument("--extreme-gap-abs-pct", type=float, default=30.0)
    parser.add_argument("--extreme-range-pct", type=float, default=50.0)
    parser.add_argument("--volume-spike-multiple", type=float, default=10.0)
    parser.add_argument("--min-price-threshold", type=float, default=None)
    parser.add_argument("--focus-timestamp", default="2026-01-03T23:00:00Z")
    args = parser.parse_args(argv)

    if args.network not in ("testnet", "mainnet"):
        raise SystemExit("network must be testnet or mainnet")

    print("historical_data_network=" + args.network)
    df = load_historical_candles(
        coin=args.coin,
        interval=args.interval,
        start=args.start,
        end=args.end,
        network=args.network,
    )
    thresholds = DataQualityThresholds(
        extreme_bar_return_abs_pct=args.extreme_bar_return_abs_pct,
        extreme_gap_abs_pct=args.extreme_gap_abs_pct,
        extreme_range_pct=args.extreme_range_pct,
        volume_spike_multiple=args.volume_spike_multiple,
        close_below_min_price_threshold=args.min_price_threshold,
    )
    report = audit_ohlcv_quality(df, interval=args.interval, thresholds=thresholds)
    run_id = build_run_id(args.coin, args.interval)
    paths = write_history_quality_report(
        report=report,
        df=df,
        output_dir=Path("reports/backtests") / args.coin.lower(),
        coin=args.coin,
        interval=args.interval,
        run_id=run_id,
        focus_timestamp=args.focus_timestamp,
    )

    print("HISTORY_QUALITY_AUDIT_COMPLETE")
    print("run_id=" + run_id)
    print("warning_count=" + str(report.warning_count))
    print("largest_bar_return=" + str(report.largest_bar_return_abs_pct))
    print("largest_gap=" + str(report.largest_gap_abs_pct))
    print("largest_range=" + str(report.largest_range_pct))
    print("first_flagged_timestamp=" + str(report.first_flagged_timestamp))
    print("csv=" + str(paths["csv"]))
    print("json=" + str(paths["json"]))
    print("md=" + str(paths["md"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
