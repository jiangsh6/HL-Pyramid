from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.backtest.historical_loader import execution_network, historical_data_network, load_historical_candles
from src.backtest.parameter_grid import apply_parameter_set, config_to_raw, parameter_combinations
from src.backtest.pyramiding_backtester import run_research_backtest
from src.core.config_loader import load_config
from src.core.models import BotConfig


def _rank(row: dict) -> tuple:
    return (
        float(row["excess_return_vs_buy_hold_pct"]) > 0,
        float(row["max_drawdown_pct"]) > float(row["buy_and_hold_max_drawdown_pct"]),
        -abs(float(row["trade_count"]) - 8),
        float(row["profit_factor"]),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sweep HYPE strategy parameters")
    parser.add_argument("--base-config", required=True)
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--max-runs", type=int, default=0, help="Test helper; 0 means full grid")
    args = parser.parse_args(argv)

    base_config = load_config(args.base_config)
    raw_base = config_to_raw(base_config)
    coin = str((base_config.hl or {}).get("coin", "HYPE"))
    exec_network = execution_network(base_config)
    data_network = historical_data_network(base_config)
    print("execution_network=" + exec_network)
    print("historical_data_network=" + data_network)
    df = load_historical_candles(coin=coin, interval=args.interval, start=args.start, end=args.end, network=data_network)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path("reports/backtests") / coin.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    combos = parameter_combinations()
    if args.max_runs and args.max_runs > 0:
        combos = combos[: args.max_runs]
    for idx, params in enumerate(combos):
        raw = apply_parameter_set(raw_base, params)
        cfg = BotConfig(**raw)
        result = run_research_backtest(
            df=df,
            config=cfg,
            coin=coin,
            interval=args.interval,
            run_dir=out_dir / f"sweep_{run_id}_{idx:04d}",
            run_id=f"sweep_{run_id}_{idx:04d}",
        )
        summary = result.summary
        rows.append({
            "run_index": idx,
            "params_json": json.dumps(params, sort_keys=True),
            "total_return_pct": summary["total_return_pct"],
            "buy_and_hold_return_pct": summary["buy_and_hold_return_pct"],
            "excess_return_vs_buy_hold_pct": summary["excess_return_vs_buy_hold_pct"],
            "max_drawdown_pct": summary["max_drawdown_pct"],
            "buy_and_hold_max_drawdown_pct": summary["buy_and_hold_max_drawdown_pct"],
            "trade_count": summary["number_of_entries"] + summary["number_of_exits"],
            "add_count": summary["number_of_adds"],
            "profit_factor": summary["profit_factor"],
        })
    rows = sorted(rows, key=_rank, reverse=True)
    csv_path = out_dir / f"parameter_sweep_{run_id}.csv"
    json_path = out_dir / f"parameter_sweep_{run_id}.json"
    md_path = out_dir / f"parameter_sweep_{run_id}.md"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["run_index"])
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps({"runs": rows}, indent=2, sort_keys=True))
    md_path.write_text("# HYPE Parameter Sweep\n\n" + (f"Best run: `{rows[0]['params_json']}`\n" if rows else "No runs.\n"))
    print("PARAMETER_SWEEP_COMPLETE")
    print("csv=" + str(csv_path))
    print("json=" + str(json_path))
    print("markdown=" + str(md_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
