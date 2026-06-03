from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import yaml

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.backtest.data_quality import audit_ohlcv_quality  # noqa: E402
from src.backtest.historical_loader import execution_network, historical_data_network, load_historical_candles  # noqa: E402
from src.backtest.parameter_grid import apply_parameter_set, config_to_raw  # noqa: E402
from src.backtest.pyramiding_backtester import run_research_backtest  # noqa: E402
from src.core.config_loader import load_config  # noqa: E402
from src.core.models import BotConfig  # noqa: E402


DISTANCE_ABOVE_MA20_LIMITS = [0.05, 0.10, 0.15, 0.20, 0.30]
VOLUME_RATIO_20D_MIN = [0.5, 0.8, 1.0, 1.2]
TRAILING_STOP_PCTS = [0.10, 0.15, 0.20, 0.25, 0.30]
MA20_EXIT_ENABLED = [False, True]
ADD_PROFIT_THRESHOLDS = [0.10, 0.20, 0.35]


def build_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else ["status"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))


def calibration_grid() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for distance in DISTANCE_ABOVE_MA20_LIMITS:
        for volume_min in VOLUME_RATIO_20D_MIN:
            for trailing in TRAILING_STOP_PCTS:
                for ma20_exit in MA20_EXIT_ENABLED:
                    for add_threshold in ADD_PROFIT_THRESHOLDS:
                        rows.append({
                            "entry.starter.max_distance_above_ma20_pct": distance,
                            "entry.starter.max_distance_above_ma10_pct": distance,
                            "entry.starter.min_volume_vs_20d_avg": volume_min,
                            "risk.trailing_stop.default_trailing_pct": trailing,
                            "reduce.enabled": ma20_exit,
                            "reduce.base_reduce.reduce_base_half_if_close_below_ma20": ma20_exit,
                            "add.min_unrealized_profit_before_first_add_pct": add_threshold,
                        })
    return rows


def _rank(row: dict[str, Any]) -> tuple[Any, ...]:
    entries = float(row["number_of_entries"])
    time_in_market = float(row["time_in_market_pct"])
    return (
        entries >= 10,
        time_in_market >= 20,
        float(row["total_return_pct"]) > 0,
        -abs(float(row["max_drawdown_pct"])),
        float(row["excess_return_vs_buy_hold_pct"]),
        float(row["total_return_pct"]),
        entries,
        time_in_market,
    )


def top_blockers_from_signal_csv(path: Path) -> tuple[list[tuple[str, int]], dict[str, Any]]:
    blocker_counts: Counter[str] = Counter()
    readiness_values: list[float] = []
    if not path.exists():
        return [], {"count": 0}
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            for blocker in str(row.get("blockers", "")).split("|"):
                if blocker:
                    blocker_counts[blocker] += 1
            try:
                readiness_values.append(float(row.get("readiness_score") or 0))
            except ValueError:
                pass
    readiness = {
        "count": len(readiness_values),
        "avg": sum(readiness_values) / len(readiness_values) if readiness_values else 0.0,
        "min": min(readiness_values) if readiness_values else 0.0,
        "max": max(readiness_values) if readiness_values else 0.0,
        "gte_80": sum(1 for value in readiness_values if value >= 80),
        "gte_90": sum(1 for value in readiness_values if value >= 90),
        "eq_100": sum(1 for value in readiness_values if value == 100),
    }
    return blocker_counts.most_common(15), readiness


def run_candidate(
    *,
    idx: int,
    params: dict[str, Any],
    raw_base: dict[str, Any],
    df: pd.DataFrame,
    coin: str,
    interval: str,
    out_dir: Path,
    quality_summary: dict[str, Any],
) -> dict[str, Any]:
    cfg = BotConfig(**apply_parameter_set(raw_base, params))
    run_id = f"calibration_{idx:04d}"
    run_dir = out_dir / run_id
    result = run_research_backtest(
        df=df,
        config=cfg,
        coin=coin,
        interval=interval,
        run_dir=run_dir,
        run_id=run_id,
        data_quality=quality_summary,
    )
    summary = result.summary
    starting_equity = float(cfg.capital["starting_equity"])
    top_blockers, readiness = top_blockers_from_signal_csv(run_dir / "signal_diagnostics.csv")
    orders = result.orders
    sell_returns = [float(order.get("return_pct", 0.0)) for order in orders if order.get("side") == "sell"]
    return {
        "run_index": idx,
        "params_json": json.dumps(params, sort_keys=True),
        "total_return_pct": summary["total_return_pct"],
        "buy_and_hold_return_pct": summary["buy_and_hold_return_pct"],
        "excess_return_vs_buy_hold_pct": summary["excess_return_vs_buy_hold_pct"],
        "max_drawdown_pct": summary["max_drawdown_pct"],
        "number_of_entries": summary["number_of_entries"],
        "number_of_adds": summary["number_of_adds"],
        "number_of_stops": summary["number_of_stops"],
        "time_in_market_pct": summary["time_in_market_pct"],
        "win_rate": summary.get("win_rate", 0.0),
        "average_trade_return_pct": sum(sell_returns) / len(sell_returns) if sell_returns else 0.0,
        "final_equity": starting_equity * (1.0 + float(summary["total_return_pct"]) / 100.0),
        "profit_factor": summary.get("profit_factor", 0.0),
        "top_blockers_json": json.dumps(top_blockers),
        "readiness_json": json.dumps(readiness, sort_keys=True),
    }


def generate_candidate_config(
    *,
    base_config_path: str,
    best_params: dict[str, Any],
    output_path: Path,
) -> None:
    raw = yaml.safe_load(Path(base_config_path).read_text())
    for dotted, value in best_params.items():
        cur = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            cur = cur[part]
        cur[parts[-1]] = value
    raw["bot"]["name"] = "hype_candidate_loose_v1"
    raw["bot"]["version"] = "0.3"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(raw, sort_keys=False))


def write_report(
    *,
    out_dir: Path,
    rows: list[dict[str, Any]],
    best: dict[str, Any] | None,
) -> None:
    lines = [
        "# HYPE Strategy Calibration",
        "",
        "Focused sweep against the existing starter/add/stop strategy. This is research-only.",
        "",
    ]
    if best:
        lines.extend([
            "## Best Candidate",
            "",
            f"- Run index: `{best['run_index']}`",
            f"- Total return pct: `{best['total_return_pct']}`",
            f"- Buy and hold return pct: `{best['buy_and_hold_return_pct']}`",
            f"- Excess return pct: `{best['excess_return_vs_buy_hold_pct']}`",
            f"- Max drawdown pct: `{best['max_drawdown_pct']}`",
            f"- Entries: `{best['number_of_entries']}`",
            f"- Adds: `{best['number_of_adds']}`",
            f"- Stops: `{best['number_of_stops']}`",
            f"- Time in market pct: `{best['time_in_market_pct']}`",
            f"- Params: `{best['params_json']}`",
            "",
            "## Blocker Diagnostics",
            "",
            f"- Top blockers: `{best['top_blockers_json']}`",
            f"- Readiness distribution: `{best['readiness_json']}`",
            "",
        ])
    lines.extend([
        "## Ranking Rules",
        "",
        "1. Prefer at least 10 entries.",
        "2. Prefer at least 20% time in market.",
        "3. Prefer positive total return.",
        "4. Prefer controlled drawdown.",
        "5. Prefer excess return, but do not require it for HYPE yet.",
    ])
    (out_dir / "calibration_report.md").write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate HYPE starter/add/stop parameters for research")
    parser.add_argument("--coin", default="HYPE")
    parser.add_argument("--base-config", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--max-runs", type=int, default=50)
    args = parser.parse_args(argv)

    config = load_config(args.base_config)
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
    quality = audit_ohlcv_quality(df, interval=args.interval)
    quality_summary = quality.summary(policy="included")
    raw_base = config_to_raw(config)
    run_id = build_run_id()
    out_dir = Path("reports/research") / args.coin.lower() / f"calibration_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    grid = calibration_grid()
    if args.max_runs > 0:
        grid = grid[:args.max_runs]

    rows = [
        run_candidate(
            idx=idx,
            params=params,
            raw_base=raw_base,
            df=df,
            coin=args.coin,
            interval=args.interval,
            out_dir=out_dir,
            quality_summary=quality_summary,
        )
        for idx, params in enumerate(grid)
    ]
    ranked = sorted(rows, key=_rank, reverse=True)
    write_csv(out_dir / "calibration_summary.csv", ranked)
    write_json(out_dir / "calibration_summary.json", {"runs": ranked, "data_quality": quality_summary})
    best = ranked[0] if ranked else None
    if best:
        generate_candidate_config(
            base_config_path=args.base_config,
            best_params=json.loads(best["params_json"]),
            output_path=Path("config/hype_candidate_loose_v1.yaml"),
        )
    write_report(out_dir=out_dir, rows=ranked, best=best)

    print("CALIBRATION_COMPLETE")
    print("run_dir=" + str(out_dir))
    if best:
        print("best_run_index=" + str(best["run_index"]))
        print("best_entries=" + str(best["number_of_entries"]))
        print("best_time_in_market_pct=" + str(best["time_in_market_pct"]))
        print("best_total_return_pct=" + str(best["total_return_pct"]))
        print("candidate_config=config/hype_candidate_loose_v1.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
