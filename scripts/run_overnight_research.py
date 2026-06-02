from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.backtest.data_quality import (  # noqa: E402
    DataQualityReport,
    audit_ohlcv_quality,
    filter_flagged_candles,
    write_history_quality_report,
)
from src.backtest.historical_loader import execution_network, historical_data_network, load_historical_candles  # noqa: E402
from src.backtest.parameter_grid import apply_parameter_set, config_to_raw, parameter_combinations  # noqa: E402
from src.backtest.pyramiding_backtester import run_research_backtest  # noqa: E402
from src.core.config_loader import load_config  # noqa: E402
from src.core.models import BotConfig  # noqa: E402


SEVERE_ISSUE_TYPES = {
    "extreme_bar_return",
    "extreme_gap_return",
    "extreme_high_low_range",
    "suspected_market_reset",
}


SIGNAL_NAMES = [
    "close_above_ma20",
    "close_above_ma50",
    "ma20_above_ma50",
    "20_bar_breakout",
    "volume_confirmed_trend",
]


def build_run_id(coin: str) -> str:
    return f"{coin.lower()}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


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


def _safe_float(value: Any) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def issue_counts(report: DataQualityReport) -> dict[str, int]:
    counts = {
        "missing_candle_count": 0,
        "duplicate_timestamp_count": 0,
        "zero_volume_count": 0,
        "severe_issue_count": 0,
    }
    for issue in report.issues:
        if issue.issue_type == "missing_candles":
            counts["missing_candle_count"] += int(issue.value)
        if issue.issue_type == "duplicate_timestamp":
            counts["duplicate_timestamp_count"] += 1
        if issue.issue_type == "zero_volume":
            counts["zero_volume_count"] += 1
        if issue.issue_type in SEVERE_ISSUE_TYPES:
            counts["severe_issue_count"] += 1
    return counts


def data_suitable(report: DataQualityReport) -> bool:
    return issue_counts(report)["severe_issue_count"] == 0


def severe_timestamps(report: DataQualityReport) -> list[str]:
    return sorted({issue.timestamp for issue in report.issues if issue.issue_type in SEVERE_ISSUE_TYPES})


def choose_clean_start(df: pd.DataFrame, report: DataQualityReport) -> tuple[str | None, str]:
    timestamps = severe_timestamps(report)
    if not timestamps:
        return None, "no_severe_discontinuity_found"
    last = pd.Timestamp(timestamps[-1])
    later = df.index[df.index > last]
    if len(later) == 0:
        return None, "no_candles_after_last_severe_discontinuity"
    return later[0].isoformat(), "first_candle_after_last_severe_discontinuity"


def copy_quality_outputs(paths: dict[str, Path], out_dir: Path) -> dict[str, Path]:
    copied = {}
    for key, src in paths.items():
        dst = out_dir / f"history_quality.{key}"
        dst.write_text(src.read_text())
        copied[key] = dst
    return copied


def run_quality_stage(
    *,
    df: pd.DataFrame,
    coin: str,
    interval: str,
    run_id: str,
    out_dir: Path,
) -> tuple[DataQualityReport, dict[str, Any]]:
    report = audit_ohlcv_quality(df, interval=interval)
    paths = write_history_quality_report(
        report=report,
        df=df,
        output_dir=out_dir,
        coin=coin,
        interval=interval,
        run_id=run_id,
    )
    copy_quality_outputs(paths, out_dir)
    counts = issue_counts(report)
    payload = {
        **report.summary(policy="included"),
        **counts,
        "flagged_candle_count": len(report.flagged_timestamps),
        "suspected_regime_discontinuity_timestamps": [
            issue.timestamp for issue in report.issues if issue.issue_type == "suspected_market_reset"
        ],
        "data_suitable_for_research": data_suitable(report),
    }
    return report, payload


def run_baseline_stage(
    *,
    df: pd.DataFrame,
    out_dir: Path,
    clean_start_date: str | None,
) -> dict[str, Any]:
    start_price = float(df["close"].iloc[0]) if len(df) else 0.0
    end_price = float(df["close"].iloc[-1]) if len(df) else 0.0
    returns = df["close"].pct_change().dropna()
    cumulative = (df["close"] / start_price - 1.0) * 100.0 if start_price else pd.Series(dtype=float)
    drawdown = ((df["close"] / df["close"].cummax()) - 1.0) * 100.0 if len(df) else pd.Series(dtype=float)
    payload = {
        "baseline_return_pct": ((end_price / start_price) - 1.0) * 100.0 if start_price else 0.0,
        "baseline_max_drawdown_pct": float(drawdown.min()) if len(drawdown) else 0.0,
        "baseline_volatility": float(returns.std() * math.sqrt(24 * 365) * 100.0) if len(returns) > 1 else 0.0,
        "baseline_start_price": start_price,
        "baseline_end_price": end_price,
        "baseline_bar_count": len(df),
        "clean_start_date": clean_start_date,
    }
    write_json(out_dir / "baseline.json", payload)
    write_csv(out_dir / "baseline.csv", [payload])
    (out_dir / "baseline.md").write_text(
        "# Baseline\n\n"
        f"- Return pct: `{payload['baseline_return_pct']}`\n"
        f"- Max drawdown pct: `{payload['baseline_max_drawdown_pct']}`\n"
        f"- Bar count: `{payload['baseline_bar_count']}`\n"
    )
    return payload


def _timeframe_recommend(rows: list[dict[str, Any]], *, dirty_allowed: bool) -> tuple[str, str]:
    if any(int(row.get("trade_count", 0) or 0) <= 0 for row in rows):
        return "inconclusive", "inconclusive_zero_trade_timeframe"
    if not dirty_allowed and any(int(row.get("data_quality_warning_count", 0) or 0) > 0 for row in rows):
        return "inconclusive", "dirty_data_timeframe_not_recommended"
    ranked = sorted(
        rows,
        key=lambda row: (
            float(row["excess_return_vs_buy_hold_pct"]),
            -abs(float(row["max_drawdown_pct"])),
        ),
        reverse=True,
    )
    if len(ranked) < 2 or ranked[0]["excess_return_vs_buy_hold_pct"] == ranked[1]["excess_return_vs_buy_hold_pct"]:
        return "inconclusive", "inconclusive_no_clear_edge"
    return str(ranked[0]["interval"]), "recommended_by_excess_return_drawdown_trade_activity"


def run_timeframe_stage(
    *,
    config: BotConfig,
    coin: str,
    intervals: list[str],
    start: str,
    end: str,
    data_network: str,
    out_dir: Path,
    allow_dirty_data: bool,
    exclude_flagged_candles: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for interval in intervals:
        df = load_historical_candles(coin=coin, interval=interval, start=start, end=end, network=data_network)
        quality = audit_ohlcv_quality(df, interval=interval)
        policy = "excluded" if exclude_flagged_candles else "included"
        if exclude_flagged_candles:
            df = filter_flagged_candles(df, quality)
        result = run_research_backtest(
            df=df,
            config=config,
            coin=coin,
            interval=interval,
            run_dir=out_dir / f"timeframe_{interval}",
            run_id=f"timeframe_{interval}",
            data_quality=quality.summary(policy=policy),
        )
        summary = result.summary
        rows.append({
            "interval": interval,
            "total_return_pct": summary["total_return_pct"],
            "max_drawdown_pct": summary["max_drawdown_pct"],
            "buy_and_hold_return_pct": summary["buy_and_hold_return_pct"],
            "excess_return_vs_buy_hold_pct": summary["excess_return_vs_buy_hold_pct"],
            "trade_count": summary["number_of_entries"] + summary["number_of_exits"],
            "add_count": summary["number_of_adds"],
            "stop_count": summary["number_of_stops"],
            "time_in_market_pct": summary["time_in_market_pct"],
            "top_blockers": json.dumps(summary["top_blockers"], default=str),
            "average_readiness_score": summary["average_readiness_score"],
            "max_readiness_score": summary["max_readiness_score"],
            "data_quality_warning_count": summary["data_quality_warning_count"],
            "flagged_candles_policy": summary["flagged_candles_policy"],
        })
    recommendation, reason = _timeframe_recommend(rows, dirty_allowed=allow_dirty_data or exclude_flagged_candles)
    payload = {"rows": rows, "recommendation": recommendation, "recommendation_reason": reason}
    write_csv(out_dir / "timeframe_comparison.csv", rows)
    write_json(out_dir / "timeframe_comparison.json", payload)
    (out_dir / "timeframe_comparison.md").write_text(
        "# Timeframe Comparison\n\n"
        + "\n".join(f"- {row['interval']}: trades={row['trade_count']}, return={row['total_return_pct']}" for row in rows)
        + f"\n\nRecommendation: `{recommendation}`\nReason: `{reason}`\n"
    )
    return rows, payload


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ma20"] = out["close"].rolling(20).mean()
    out["ma50"] = out["close"].rolling(50).mean()
    out["volume_20_avg"] = out["volume"].rolling(20).mean()
    out["high_20"] = out["high"].rolling(20).max().shift(1)
    return out


def _signal_trigger(row: pd.Series, signal: str) -> bool:
    if signal == "close_above_ma20":
        return bool(row["close"] > row["ma20"])
    if signal == "close_above_ma50":
        return bool(row["close"] > row["ma50"])
    if signal == "ma20_above_ma50":
        return bool(row["ma20"] > row["ma50"])
    if signal == "20_bar_breakout":
        return bool(row["close"] > row["high_20"])
    if signal == "volume_confirmed_trend":
        return bool(row["close"] > row["ma20"] and row["volume"] > row["volume_20_avg"])
    return False


def run_signal_validation_stage(
    *,
    df: pd.DataFrame,
    quality_report: DataQualityReport,
    out_dir: Path,
) -> dict[str, Any]:
    if len(df) < 80:
        payload = {"status": "SKIPPED", "reason": "insufficient_clean_history", "rows": []}
        write_json(out_dir / "signal_validation.json", payload)
        write_csv(out_dir / "signal_validation.csv", [])
        (out_dir / "signal_validation.md").write_text("# Signal Validation\n\nSKIPPED: insufficient_clean_history\n")
        return payload

    flagged = {pd.Timestamp(ts) for ts in quality_report.flagged_timestamps}
    data = _add_indicators(df)
    rows = []
    horizons = {"6h": 6, "12h": 12, "24h": 24, "72h": 72}
    for signal in SIGNAL_NAMES:
        events = []
        for i in range(len(data) - max(horizons.values())):
            row = data.iloc[i]
            if row[["ma20", "ma50", "volume_20_avg", "high_20"]].isna().any():
                continue
            if not _signal_trigger(row, signal):
                continue
            base = float(row["close"])
            future = data.iloc[i + 1: i + max(horizons.values()) + 1]
            event = {
                "signal": signal,
                "timestamp": data.index[i],
                "data_quality_warnings_affecting_signal_window": int(data.index[i] in flagged),
                "mae_pct": ((future["low"].min() / base) - 1.0) * 100.0 if base else 0.0,
                "mfe_pct": ((future["high"].max() / base) - 1.0) * 100.0 if base else 0.0,
            }
            for label, bars in horizons.items():
                event[f"forward_return_{label}"] = ((float(data.iloc[i + bars]["close"]) / base) - 1.0) * 100.0 if base else 0.0
            events.append(event)
        for_return = [event["forward_return_24h"] for event in events]
        rows.append({
            "signal": signal,
            "trigger_events": len(events),
            "hit_rate_24h": sum(1 for value in for_return if value > 0) / len(for_return) if for_return else 0.0,
            "average_forward_return_6h": sum(event["forward_return_6h"] for event in events) / len(events) if events else 0.0,
            "average_forward_return_12h": sum(event["forward_return_12h"] for event in events) / len(events) if events else 0.0,
            "average_forward_return_24h": sum(for_return) / len(for_return) if for_return else 0.0,
            "average_forward_return_72h": sum(event["forward_return_72h"] for event in events) / len(events) if events else 0.0,
            "median_forward_return_24h": float(pd.Series(for_return).median()) if for_return else 0.0,
            "max_adverse_excursion": min((event["mae_pct"] for event in events), default=0.0),
            "max_favorable_excursion": max((event["mfe_pct"] for event in events), default=0.0),
            "data_quality_warnings_affecting_signal_window": sum(event["data_quality_warnings_affecting_signal_window"] for event in events),
        })
    payload = {"status": "COMPLETE", "rows": rows}
    write_csv(out_dir / "signal_validation.csv", rows)
    write_json(out_dir / "signal_validation.json", payload)
    (out_dir / "signal_validation.md").write_text(
        "# Signal Validation\n\n" + "\n".join(
            f"- {row['signal']}: events={row['trigger_events']}, avg_24h={row['average_forward_return_24h']}"
            for row in rows
        ) + "\n"
    )
    return payload


def _rank_sweep(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        float(row["excess_return_vs_buy_hold_pct"]) > 0,
        float(row["max_drawdown_pct"]) > float(row["buy_and_hold_max_drawdown_pct"]),
        -abs(float(row["trade_count"]) - 8),
        -float(row["trade_count"]) if float(row["trade_count"]) > 50 else 0,
        float(row["profit_factor"]),
    )


def write_skipped_report(out_dir: Path, stem: str, reason: str) -> dict[str, Any]:
    payload = {"status": "SKIPPED", "reason": reason}
    write_json(out_dir / f"{stem}.json", payload)
    write_csv(out_dir / f"{stem}.csv", [{"status": "SKIPPED", "reason": reason}])
    (out_dir / f"{stem}.md").write_text(f"# {stem.replace('_', ' ').title()}\n\nSKIPPED: {reason}\n")
    return payload


def run_parameter_sweep_stage(
    *,
    df: pd.DataFrame,
    config: BotConfig,
    coin: str,
    interval: str,
    out_dir: Path,
    max_grid_runs: int,
    allowed: bool,
    skip: bool,
    quality_summary: dict[str, Any],
) -> dict[str, Any]:
    if skip:
        return write_skipped_report(out_dir, "parameter_sweep", "skip_sweep_requested")
    if not allowed:
        return write_skipped_report(out_dir, "parameter_sweep", "dirty_data_without_override")
    raw_base = config_to_raw(config)
    combos = parameter_combinations()
    if max_grid_runs > 0:
        combos = combos[:max_grid_runs]
    rows = []
    for idx, params in enumerate(combos):
        cfg = BotConfig(**apply_parameter_set(raw_base, params))
        result = run_research_backtest(
            df=df,
            config=cfg,
            coin=coin,
            interval=interval,
            run_dir=out_dir / f"sweep_{idx:04d}",
            run_id=f"sweep_{idx:04d}",
            data_quality=quality_summary,
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
    rows = sorted(rows, key=_rank_sweep, reverse=True)
    payload = {"status": "COMPLETE", "runs": rows, "max_grid_runs": max_grid_runs}
    write_csv(out_dir / "parameter_sweep.csv", rows)
    write_json(out_dir / "parameter_sweep.json", payload)
    (out_dir / "parameter_sweep.md").write_text(
        "# Parameter Sweep\n\n" + (f"Best run: `{rows[0]['params_json']}`\n" if rows else "No runs.\n")
    )
    return payload


def run_walk_forward_stage(*, df: pd.DataFrame, out_dir: Path, skip: bool) -> dict[str, Any]:
    if skip:
        return write_skipped_report(out_dir, "walk_forward", "skip_walk_forward_requested")
    if len(df) < 24 * 44:
        return write_skipped_report(out_dir, "walk_forward", "insufficient_clean_history_for_30d_train_7d_test")
    payload = {
        "status": "SCAFFOLD_READY",
        "train_days": 30,
        "test_days": 7,
        "step_days": 3,
        "reason": "walk_forward_structure_recorded_full_rolling_optimizer_deferred",
    }
    write_json(out_dir / "walk_forward.json", payload)
    write_csv(out_dir / "walk_forward.csv", [payload])
    (out_dir / "walk_forward.md").write_text("# Walk Forward\n\nSCAFFOLD_READY\n")
    return payload


def final_verdict(
    *,
    data_clean: bool,
    allow_dirty: bool,
    signal_payload: dict[str, Any],
    timeframe_payload: dict[str, Any],
) -> str:
    if not data_clean and not allow_dirty:
        return "RESEARCH_DATA_DIRTY"
    if signal_payload.get("status") == "SKIPPED":
        return "RESEARCH_NEEDS_MORE_DATA"
    rows = signal_payload.get("rows", [])
    if rows and max(_safe_float(row.get("average_forward_return_24h")) for row in rows) <= 0:
        return "RESEARCH_SIGNAL_WEAK"
    if timeframe_payload.get("recommendation") == "inconclusive":
        return "RESEARCH_NEEDS_MORE_DATA"
    return "RESEARCH_READY_FOR_TESTNET"


def write_final_reports(
    *,
    out_dir: Path,
    payload: dict[str, Any],
) -> None:
    write_json(out_dir / "overnight_research_summary.json", payload)
    write_json(out_dir / "research_recommendation.json", payload["recommendation"])
    timeframe = payload.get("timeframe") or {}
    lines = [
        "# Overnight Research Report",
        "",
        f"- Coin: `{payload['coin']}`",
        f"- Verdict: `{payload['recommendation']['verdict']}`",
        f"- Data clean enough: `{payload['data_quality']['data_suitable_for_research']}`",
        f"- Auto clean start used: `{payload['auto_clean_start_used']}`",
        f"- Analyzed start: `{payload['selected_start']}`",
        f"- Analyzed end: `{payload['selected_end']}`",
        f"- Timeframe recommendation: `{timeframe.get('recommendation', 'not_run')}`",
        f"- Timeframe reason: `{timeframe.get('recommendation_reason', 'not_run')}`",
        "",
        "## Recommendation",
        "",
        f"Proceed: `{payload['recommendation']['proceed_to']}`",
        f"Reason: `{payload['recommendation']['reason']}`",
    ]
    (out_dir / "overnight_research_report.md").write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run overnight research waterfall for a Hyperliquid perp ticker")
    parser.add_argument("--coin", default="HYPE")
    parser.add_argument("--base-config", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--intervals", default="1h,4h")
    parser.add_argument("--skip-sweep", action="store_true")
    parser.add_argument("--skip-walk-forward", action="store_true")
    parser.add_argument("--max-grid-runs", type=int, default=100)
    parser.add_argument("--allow-dirty-data", action="store_true")
    parser.add_argument("--exclude-flagged-candles", action="store_true")
    parser.add_argument("--auto-clean-start", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(args.base_config)
    exec_network = execution_network(config)
    data_network = historical_data_network(config)
    print("execution_network=" + exec_network)
    print("historical_data_network=" + data_network)
    run_id = build_run_id(args.coin)
    out_dir = Path("reports/research") / args.coin.lower() / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    intervals = [part.strip() for part in args.intervals.split(",") if part.strip()]
    primary_interval = intervals[0]

    original_df = load_historical_candles(
        coin=args.coin,
        interval=primary_interval,
        start=args.start,
        end=args.end,
        network=data_network,
    )
    quality_report, quality_payload = run_quality_stage(
        df=original_df,
        coin=args.coin,
        interval=primary_interval,
        run_id=run_id,
        out_dir=out_dir,
    )
    data_clean = data_suitable(quality_report)
    selected_start = args.start
    clean_start_date = None
    auto_clean_start_used = False
    df = original_df

    if not data_clean and args.auto_clean_start:
        clean_start_date, reason = choose_clean_start(original_df, quality_report)
        quality_payload["clean_start_reason"] = reason
        if clean_start_date:
            selected_start = clean_start_date
            auto_clean_start_used = True
            df = original_df.loc[original_df.index >= pd.Timestamp(clean_start_date)].copy()
            quality_report = audit_ohlcv_quality(df, interval=primary_interval)
            quality_payload.update(run_quality_stage(
                df=df,
                coin=args.coin,
                interval=primary_interval,
                run_id=f"{run_id}_clean",
                out_dir=out_dir,
            )[1])

    if args.exclude_flagged_candles:
        df = filter_flagged_candles(df, quality_report)
        quality_policy = "excluded"
    else:
        quality_policy = "included"
    quality_summary = quality_report.summary(policy=quality_policy)

    if not data_suitable(quality_report) and not args.allow_dirty_data and not auto_clean_start_used:
        recommendation = {
            "verdict": "RESEARCH_DATA_DIRTY",
            "proceed_to": "more_research",
            "reason": "severe_data_quality_issues_block_default_pipeline",
        }
        payload = {
            "coin": args.coin,
            "run_id": run_id,
            "execution_network": exec_network,
            "historical_data_network": data_network,
            "selected_start": selected_start,
            "selected_end": args.end,
            "auto_clean_start_used": auto_clean_start_used,
            "data_quality": quality_payload,
            "baseline": None,
            "timeframe": None,
            "signal_validation": None,
            "parameter_sweep": None,
            "walk_forward": None,
            "recommendation": recommendation,
        }
        write_final_reports(out_dir=out_dir, payload=payload)
        print("OVERNIGHT_RESEARCH_COMPLETE")
        print("run_dir=" + str(out_dir))
        print("verdict=RESEARCH_DATA_DIRTY")
        return 0

    baseline = run_baseline_stage(df=df, out_dir=out_dir, clean_start_date=clean_start_date)
    timeframe_rows, timeframe_payload = run_timeframe_stage(
        config=config,
        coin=args.coin,
        intervals=intervals,
        start=selected_start,
        end=args.end,
        data_network=data_network,
        out_dir=out_dir,
        allow_dirty_data=args.allow_dirty_data,
        exclude_flagged_candles=args.exclude_flagged_candles,
    )
    signal_payload = run_signal_validation_stage(df=df, quality_report=quality_report, out_dir=out_dir)
    sweep_allowed = data_suitable(quality_report) or args.allow_dirty_data or auto_clean_start_used
    sweep_payload = run_parameter_sweep_stage(
        df=df,
        config=config,
        coin=args.coin,
        interval=primary_interval,
        out_dir=out_dir,
        max_grid_runs=args.max_grid_runs,
        allowed=sweep_allowed,
        skip=args.skip_sweep,
        quality_summary=quality_summary,
    )
    walk_payload = run_walk_forward_stage(df=df, out_dir=out_dir, skip=args.skip_walk_forward)
    verdict = final_verdict(
        data_clean=data_suitable(quality_report),
        allow_dirty=args.allow_dirty_data or auto_clean_start_used,
        signal_payload=signal_payload,
        timeframe_payload=timeframe_payload,
    )
    proceed_to = "VPS testnet live" if verdict == "RESEARCH_READY_FOR_TESTNET" else "more_research"
    recommendation = {
        "verdict": verdict,
        "proceed_to": proceed_to,
        "reason": timeframe_payload.get("recommendation_reason", "research_pipeline_completed"),
        "overfit_risk": "unknown_until_walk_forward_is_fully_implemented",
        "current_config_acceptable": verdict == "RESEARCH_READY_FOR_TESTNET",
        "parameters_to_adjust": "see parameter_sweep.json" if sweep_payload.get("status") == "COMPLETE" else "not_available",
    }
    payload = {
        "coin": args.coin,
        "run_id": run_id,
        "execution_network": exec_network,
        "historical_data_network": data_network,
        "selected_start": selected_start,
        "selected_end": args.end,
        "auto_clean_start_used": auto_clean_start_used,
        "data_quality": quality_payload,
        "baseline": baseline,
        "timeframe": timeframe_payload,
        "timeframe_rows": timeframe_rows,
        "signal_validation": signal_payload,
        "parameter_sweep": sweep_payload,
        "walk_forward": walk_payload,
        "recommendation": recommendation,
    }
    write_final_reports(out_dir=out_dir, payload=payload)
    print("OVERNIGHT_RESEARCH_COMPLETE")
    print("run_dir=" + str(out_dir))
    print("verdict=" + verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
