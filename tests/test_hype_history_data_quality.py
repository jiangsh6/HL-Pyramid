from __future__ import annotations

import json

import pandas as pd

from src.backtest.data_quality import (
    DataQualityThresholds,
    audit_ohlcv_quality,
    filter_flagged_candles,
    write_history_quality_report,
)
from src.backtest.pyramiding_backtester import run_research_backtest
from src.core.config_loader import load_config


TESTNET_WALLET = "0x1111111111111111111111111111111111111111"


def _quality_df(rows: int = 12) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=rows, freq="1h", tz="UTC")
    close = [10.0 + i * 0.1 for i in range(rows)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c * 1.02 for c in close],
            "low": [c * 0.98 for c in close],
            "close": close,
            "volume": [1000.0 + i for i in range(rows)],
        },
        index=idx,
    )


def _backtest_df(rows: int = 90) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=rows, freq="1h", tz="UTC")
    close = [10 + i * 0.03 for i in range(rows)]
    return pd.DataFrame(
        {
            "open": [c * 0.999 for c in close],
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "adj_close": close,
            "volume": [1000 + i for i in range(rows)],
        },
        index=idx,
    )


def _cfg(monkeypatch):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    cfg = load_config("config/hype_candidate_normal_v2.yaml")
    cfg.entry["starter"]["min_volume_vs_20d_avg"] = 0.8
    return cfg


def test_detects_extreme_return_candle():
    df = _quality_df()
    df.loc[df.index[5], "close"] = 1.0
    df.loc[df.index[5], "open"] = 1.0
    df.loc[df.index[5], "high"] = 1.1
    df.loc[df.index[5], "low"] = 0.9

    report = audit_ohlcv_quality(df, interval="1h")

    assert any(issue.issue_type == "extreme_bar_return" for issue in report.issues)
    assert report.warning_count > 0


def test_detects_zero_negative_ohlc_and_high_low_inconsistency():
    df = _quality_df()
    df.loc[df.index[2], "open"] = 0.0
    df.loc[df.index[3], "high"] = 8.0
    df.loc[df.index[3], "low"] = 9.0
    df.loc[df.index[4], "close"] = 999.0

    report = audit_ohlcv_quality(df, interval="1h")
    types = {issue.issue_type for issue in report.issues}

    assert "non_positive_open" in types
    assert "high_below_low" in types
    assert "close_outside_high_low" in types


def test_detects_duplicate_timestamp():
    df = _quality_df()
    duplicated = pd.concat([df, df.iloc[[3]]]).sort_index()

    report = audit_ohlcv_quality(duplicated, interval="1h")

    assert any(issue.issue_type == "duplicate_timestamp" for issue in report.issues)


def test_history_quality_report_writes_csv_json_md(tmp_path):
    df = _quality_df()
    df.loc[df.index[6], "close"] = 1.0
    report = audit_ohlcv_quality(df, interval="1h")

    paths = write_history_quality_report(
        report=report,
        df=df,
        output_dir=tmp_path,
        coin="HYPE",
        interval="1h",
        run_id="quality_test",
        focus_timestamp=df.index[6].isoformat(),
    )

    assert paths["csv"].exists()
    assert paths["json"].exists()
    assert paths["md"].exists()
    payload = json.loads(paths["json"].read_text())
    assert payload["summary"]["data_quality_warning_count"] == report.warning_count
    assert payload["focus_context"]


def test_filtering_is_explicit_only():
    df = _quality_df()
    df.loc[df.index[5], "close"] = 1.0
    report = audit_ohlcv_quality(df, interval="1h")

    assert len(df) == 12
    filtered = filter_flagged_candles(df, report)
    assert len(filtered) < len(df)


def test_backtest_summary_includes_data_quality_warning_count(monkeypatch, tmp_path):
    cfg = _cfg(monkeypatch)
    quality = {
        "data_quality_warning_count": 3,
        "largest_bar_return": 93.7,
        "largest_gap": 10.0,
        "largest_range": 51.0,
        "first_flagged_timestamp": "2026-01-03T23:00:00+00:00",
        "flagged_candles_policy": "included",
    }

    result = run_research_backtest(
        df=_backtest_df(),
        config=cfg,
        coin="HYPE",
        interval="1h",
        run_dir=tmp_path,
        run_id="quality_backtest",
        data_quality=quality,
    )

    summary = json.loads((tmp_path / "backtest_summary.json").read_text())
    assert result.summary["data_quality_warning_count"] == 3
    assert summary["flagged_candles_policy"] == "included"
    assert "Data Quality" in (tmp_path / "backtest_report.md").read_text()
