from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_backtest_outputs(
    *,
    run_dir: Path,
    orders: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    signal_diagnostics: list[dict[str, Any]],
    equity_curve: pd.DataFrame,
    summary: dict[str, Any],
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    write_csv(run_dir / "orders.csv", orders)
    write_csv(run_dir / "positions.csv", positions)
    write_csv(run_dir / "decisions.csv", decisions)
    write_csv(run_dir / "signal_diagnostics.csv", signal_diagnostics)
    equity_curve.to_csv(run_dir / "equity_curve.csv", index=False)
    (run_dir / "backtest_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str))
    (run_dir / "backtest_report.md").write_text(format_backtest_report(summary))


def format_backtest_report(summary: dict[str, Any]) -> str:
    lines = [
        "# HYPE Backtest Report",
        "",
        f"Strategy scope: `{summary.get('strategy_scope', 'unknown')}`",
        "",
        "## Metrics",
    ]
    for key in (
        "total_return_pct",
        "buy_and_hold_return_pct",
        "excess_return_vs_buy_hold_pct",
        "max_drawdown_pct",
        "buy_and_hold_max_drawdown_pct",
        "number_of_entries",
        "number_of_adds",
        "number_of_stops",
        "time_in_market_pct",
        "average_readiness_score",
    ):
        lines.append(f"- `{key}`: {summary.get(key)}")
    lines.extend(["", "## Data Quality"])
    for key in (
        "data_quality_warning_count",
        "largest_bar_return",
        "largest_gap",
        "largest_range",
        "first_flagged_timestamp",
        "flagged_candles_policy",
    ):
        lines.append(f"- `{key}`: {summary.get(key)}")
    lines.extend(["", "## Known Scope", "Starter entry, add logic, and stop infrastructure only."])
    return "\n".join(lines) + "\n"
