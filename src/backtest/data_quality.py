from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd


@dataclass(frozen=True)
class DataQualityThresholds:
    extreme_bar_return_abs_pct: float = 30.0
    extreme_gap_abs_pct: float = 30.0
    extreme_range_pct: float = 50.0
    volume_spike_multiple: float = 10.0
    close_below_min_price_threshold: Optional[float] = None


@dataclass(frozen=True)
class DataQualityIssue:
    timestamp: str
    issue_type: str
    severity: str
    value: Any
    threshold: Any
    message: str


@dataclass(frozen=True)
class DataQualityReport:
    thresholds: DataQualityThresholds
    issues: list[DataQualityIssue]
    warning_count: int
    largest_bar_return_abs_pct: float
    largest_gap_abs_pct: float
    largest_range_pct: float
    first_flagged_timestamp: Optional[str]
    flagged_timestamps: list[str]

    def summary(self, *, policy: str = "included") -> dict[str, Any]:
        return {
            "data_quality_warning_count": self.warning_count,
            "largest_bar_return": self.largest_bar_return_abs_pct,
            "largest_gap": self.largest_gap_abs_pct,
            "largest_range": self.largest_range_pct,
            "first_flagged_timestamp": self.first_flagged_timestamp,
            "flagged_candles_policy": policy,
        }


def _timestamp(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _interval_delta(interval: str) -> pd.Timedelta:
    value = interval.strip().lower()
    if value.endswith("h"):
        return pd.Timedelta(hours=float(value[:-1]))
    if value.endswith("m"):
        return pd.Timedelta(minutes=float(value[:-1]))
    if value.endswith("d"):
        return pd.Timedelta(days=float(value[:-1]))
    raise ValueError(f"unsupported_interval_for_quality_audit:{interval}")


def _add_issue(
    issues: list[DataQualityIssue],
    *,
    timestamp: Any,
    issue_type: str,
    value: Any,
    threshold: Any,
    message: str,
    severity: str = "warning",
) -> None:
    issues.append(DataQualityIssue(
        timestamp=_timestamp(timestamp),
        issue_type=issue_type,
        severity=severity,
        value=value,
        threshold=threshold,
        message=message,
    ))


def audit_ohlcv_quality(
    df: pd.DataFrame,
    *,
    interval: str,
    thresholds: DataQualityThresholds | None = None,
) -> DataQualityReport:
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("historical candles must use a DatetimeIndex")
    thresholds = thresholds or DataQualityThresholds()
    issues: list[DataQualityIssue] = []
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"historical candles missing required columns: {sorted(missing)}")

    duplicates = df.index[df.index.duplicated()]
    for ts in duplicates:
        _add_issue(
            issues,
            timestamp=ts,
            issue_type="duplicate_timestamp",
            value=_timestamp(ts),
            threshold="unique_timestamp",
            message="duplicate historical candle timestamp",
        )

    if not df.index.is_monotonic_increasing:
        _add_issue(
            issues,
            timestamp=df.index[0] if len(df.index) else "unknown",
            issue_type="non_monotonic_timestamps",
            value="non_monotonic",
            threshold="strictly_increasing",
            message="historical candle timestamps are not monotonic increasing",
        )

    ordered = df.sort_index()
    expected_delta = _interval_delta(interval)
    diffs = ordered.index.to_series().diff()
    for ts, delta in diffs.dropna().items():
        if delta > expected_delta * 1.5:
            missing_count = max(1, int(round(delta / expected_delta)) - 1)
            _add_issue(
                issues,
                timestamp=ts,
                issue_type="missing_candles",
                value=missing_count,
                threshold=str(expected_delta),
                message=f"gap of {delta} implies {missing_count} missing candle(s)",
            )

    ohlc = ["open", "high", "low", "close"]
    for col in ohlc:
        bad = ordered[ordered[col] <= 0]
        for ts, row in bad.iterrows():
            _add_issue(
                issues,
                timestamp=ts,
                issue_type=f"non_positive_{col}",
                value=float(row[col]),
                threshold=">0",
                message=f"{col} is zero or negative",
            )

    high_low_bad = ordered[ordered["high"] < ordered["low"]]
    for ts, row in high_low_bad.iterrows():
        _add_issue(
            issues,
            timestamp=ts,
            issue_type="high_below_low",
            value={"high": float(row["high"]), "low": float(row["low"])},
            threshold="high>=low",
            message="high is below low",
        )

    for col in ("open", "close"):
        outside = ordered[(ordered[col] > ordered["high"]) | (ordered[col] < ordered["low"])]
        for ts, row in outside.iterrows():
            _add_issue(
                issues,
                timestamp=ts,
                issue_type=f"{col}_outside_high_low",
                value={col: float(row[col]), "high": float(row["high"]), "low": float(row["low"])},
                threshold="low<=value<=high",
                message=f"{col} is outside candle high/low",
            )

    bar_returns = ordered["close"].pct_change() * 100.0
    gap_returns = ((ordered["open"] - ordered["close"].shift(1)) / ordered["close"].shift(1)) * 100.0
    ranges = ((ordered["high"] - ordered["low"]) / ordered["close"].replace(0, pd.NA)) * 100.0

    for ts, value in bar_returns.dropna().items():
        value_f = float(value)
        if abs(value_f) > thresholds.extreme_bar_return_abs_pct:
            _add_issue(
                issues,
                timestamp=ts,
                issue_type="extreme_bar_return",
                value=value_f,
                threshold=thresholds.extreme_bar_return_abs_pct,
                message="close-to-close return exceeds configured threshold",
            )

    for ts, value in gap_returns.dropna().items():
        value_f = float(value)
        if abs(value_f) > thresholds.extreme_gap_abs_pct:
            _add_issue(
                issues,
                timestamp=ts,
                issue_type="extreme_gap_return",
                value=value_f,
                threshold=thresholds.extreme_gap_abs_pct,
                message="open-to-previous-close gap exceeds configured threshold",
            )

    for ts, value in ranges.dropna().items():
        value_f = float(value)
        if value_f > thresholds.extreme_range_pct:
            _add_issue(
                issues,
                timestamp=ts,
                issue_type="extreme_high_low_range",
                value=value_f,
                threshold=thresholds.extreme_range_pct,
                message="high-low candle range exceeds configured threshold",
            )

    zero_volume = ordered[ordered["volume"] <= 0]
    for ts, row in zero_volume.iterrows():
        _add_issue(
            issues,
            timestamp=ts,
            issue_type="zero_volume",
            value=float(row["volume"]),
            threshold=">0",
            message="volume is zero or negative",
        )

    volume_baseline = ordered["volume"].rolling(20, min_periods=5).median().shift(1)
    volume_multiple = ordered["volume"] / volume_baseline.replace(0, pd.NA)
    for ts, value in volume_multiple.dropna().items():
        value_f = float(value)
        if value_f > thresholds.volume_spike_multiple:
            _add_issue(
                issues,
                timestamp=ts,
                issue_type="suspicious_volume_spike",
                value=value_f,
                threshold=thresholds.volume_spike_multiple,
                message="volume is unusually high relative to rolling median",
            )

    if thresholds.close_below_min_price_threshold is not None:
        below = ordered[ordered["close"] < thresholds.close_below_min_price_threshold]
        for ts, row in below.iterrows():
            _add_issue(
                issues,
                timestamp=ts,
                issue_type="close_below_min_price_threshold",
                value=float(row["close"]),
                threshold=thresholds.close_below_min_price_threshold,
                message="close is below configured minimum price threshold",
            )

    reset_candidates = set()
    reset_candidates.update(str(ts) for ts, value in bar_returns.dropna().items() if abs(float(value)) > 80.0)
    reset_candidates.update(str(ts) for ts, value in gap_returns.dropna().items() if abs(float(value)) > 80.0)
    for ts_text in sorted(reset_candidates):
        _add_issue(
            issues,
            timestamp=ts_text,
            issue_type="suspected_market_reset",
            value="extreme_discontinuity",
            threshold="abs(return_or_gap)>80%",
            message="price discontinuity is large enough to suggest a market reset or bad candle",
            severity="critical",
        )

    flagged = sorted({issue.timestamp for issue in issues})
    first_flagged = flagged[0] if flagged else None
    return DataQualityReport(
        thresholds=thresholds,
        issues=issues,
        warning_count=len(issues),
        largest_bar_return_abs_pct=float(bar_returns.abs().max(skipna=True) or 0.0),
        largest_gap_abs_pct=float(gap_returns.abs().max(skipna=True) or 0.0),
        largest_range_pct=float(ranges.max(skipna=True) or 0.0),
        first_flagged_timestamp=first_flagged,
        flagged_timestamps=flagged,
    )


def filter_flagged_candles(df: pd.DataFrame, report: DataQualityReport) -> pd.DataFrame:
    flagged = {pd.Timestamp(ts) for ts in report.flagged_timestamps}
    return df.loc[~df.index.isin(flagged)].copy()


def surrounding_candles(
    df: pd.DataFrame,
    *,
    timestamp: str,
    flagged_timestamps: list[str],
    window: int = 5,
) -> list[dict[str, Any]]:
    if df.empty:
        return []
    target = pd.Timestamp(timestamp)
    ordered = df.sort_index()
    positions = ordered.index.get_indexer([target], method="nearest")
    pos = int(positions[0]) if len(positions) else 0
    start = max(0, pos - window)
    end = min(len(ordered), pos + window + 1)
    flagged = {pd.Timestamp(ts) for ts in flagged_timestamps}
    rows: list[dict[str, Any]] = []
    for ts, row in ordered.iloc[start:end].iterrows():
        rows.append({
            "timestamp": _timestamp(ts),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "flagged": bool(ts in flagged),
        })
    return rows


def write_history_quality_report(
    *,
    report: DataQualityReport,
    df: pd.DataFrame,
    output_dir: Path,
    coin: str,
    interval: str,
    run_id: str,
    focus_timestamp: str = "2026-01-03T23:00:00Z",
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"history_quality_{run_id}.csv"
    json_path = output_dir / f"history_quality_{run_id}.json"
    md_path = output_dir / f"history_quality_{run_id}.md"

    issue_rows = [asdict(issue) for issue in report.issues]
    with csv_path.open("w", newline="") as f:
        fieldnames = ["timestamp", "issue_type", "severity", "value", "threshold", "message"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(issue_rows)

    focus_context = surrounding_candles(
        df,
        timestamp=focus_timestamp,
        flagged_timestamps=report.flagged_timestamps,
        window=5,
    )
    payload = {
        "run_id": run_id,
        "coin": coin,
        "interval": interval,
        "summary": report.summary(policy="included"),
        "thresholds": asdict(report.thresholds),
        "issues": issue_rows,
        "focus_timestamp": focus_timestamp,
        "focus_context": focus_context,
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))

    lines = [
        f"# {coin} Historical Data Quality Audit",
        "",
        f"- Run ID: `{run_id}`",
        f"- Interval: `{interval}`",
        f"- Warning count: `{report.warning_count}`",
        f"- Largest bar return abs pct: `{report.largest_bar_return_abs_pct}`",
        f"- Largest gap abs pct: `{report.largest_gap_abs_pct}`",
        f"- Largest range pct: `{report.largest_range_pct}`",
        f"- First flagged timestamp: `{report.first_flagged_timestamp}`",
        "",
        "## Focus Context",
        "",
        f"Requested focus timestamp: `{focus_timestamp}`",
        "",
        "| timestamp | open | high | low | close | volume | flagged |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in focus_context:
        lines.append(
            f"| {row['timestamp']} | {row['open']} | {row['high']} | {row['low']} | "
            f"{row['close']} | {row['volume']} | {row['flagged']} |"
        )
    lines.extend(["", "## Issues", ""])
    if issue_rows:
        for issue in issue_rows[:50]:
            lines.append(
                f"- `{issue['timestamp']}` `{issue['issue_type']}` "
                f"value={issue['value']} threshold={issue['threshold']}"
            )
        if len(issue_rows) > 50:
            lines.append(f"- ... {len(issue_rows) - 50} additional issue(s)")
    else:
        lines.append("No quality issues found.")
    md_path.write_text("\n".join(lines) + "\n")
    return {"csv": csv_path, "json": json_path, "md": md_path}
