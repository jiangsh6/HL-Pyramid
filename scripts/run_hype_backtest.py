from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.backtest.historical_loader import load_historical_candles
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
    args = parser.parse_args(argv)

    config = load_config(args.config)
    network = str((config.hl or {}).get("network", "testnet"))
    df = load_historical_candles(
        coin=args.coin,
        interval=args.interval,
        start=args.start,
        end=args.end,
        network=network,
    )
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
    )
    print("BACKTEST_COMPLETE")
    print("run_dir=" + str(result.run_dir))
    print("total_return_pct=" + str(result.summary["total_return_pct"]))
    print("max_drawdown_pct=" + str(result.summary["max_drawdown_pct"]))
    print("strategy_scope=" + str(result.summary["strategy_scope"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
