from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.backtest.historical_loader import load_historical_candles
from src.backtest.pyramiding_backtester import run_research_backtest
from src.core.config_loader import load_config


def _recommend(rows: list[dict]) -> str:
    if len(rows) < 2:
        return "inconclusive"
    ranked = sorted(
        rows,
        key=lambda row: (
            float(row["excess_return_vs_buy_hold_pct"]),
            -abs(float(row["max_drawdown_pct"])),
        ),
        reverse=True,
    )
    if ranked[0]["excess_return_vs_buy_hold_pct"] == ranked[1]["excess_return_vs_buy_hold_pct"]:
        return "inconclusive"
    return str(ranked[0]["interval"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare HYPE 1h vs 4h research backtests")
    parser.add_argument("--config", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    coin = str((config.hl or {}).get("coin", "HYPE"))
    network = str((config.hl or {}).get("network", "testnet"))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path("reports/backtests") / coin.lower()
    rows = []
    for interval in ("1h", "4h"):
        df = load_historical_candles(coin=coin, interval=interval, start=args.start, end=args.end, network=network)
        result = run_research_backtest(
            df=df,
            config=config,
            coin=coin,
            interval=interval,
            run_dir=out_dir / f"timeframe_{interval}_{run_id}",
            run_id=f"{coin.lower()}_{interval}_{run_id}",
        )
        summary = result.summary
        rows.append({
            "interval": interval,
            "return_pct": summary["total_return_pct"],
            "max_drawdown_pct": summary["max_drawdown_pct"],
            "excess_return_vs_buy_hold_pct": summary["excess_return_vs_buy_hold_pct"],
            "trade_count": summary["number_of_entries"] + summary["number_of_exits"],
            "add_count": summary["number_of_adds"],
            "stop_count": summary["number_of_stops"],
            "time_in_market_pct": summary["time_in_market_pct"],
            "top_blockers": summary["top_blockers"],
            "average_readiness_score": summary["average_readiness_score"],
        })
    recommendation = _recommend(rows)
    csv_path = out_dir / f"timeframe_comparison_{run_id}.csv"
    md_path = out_dir / f"timeframe_comparison_{run_id}.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    md_path.write_text(
        "# HYPE Timeframe Comparison\n\n"
        + "\n".join(f"- {row['interval']}: return={row['return_pct']}, max_dd={row['max_drawdown_pct']}" for row in rows)
        + f"\n\nRecommendation: `{recommendation}`\n"
    )
    print("TIMEFRAME_COMPARISON_COMPLETE")
    print("csv=" + str(csv_path))
    print("markdown=" + str(md_path))
    print("recommendation=" + recommendation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
