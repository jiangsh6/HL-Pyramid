from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts import run_overnight_research


TESTNET_WALLET = "0x1111111111111111111111111111111111111111"
CONFIG = "/Users/shujiajiang/Crypto Dev/HL Primiding/config/hype_candidate_normal_v2.yaml"


def _clean_df(rows: int = 130, *, freq: str = "1h") -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=rows, freq=freq, tz="UTC")
    close = [10.0 + i * 0.02 for i in range(rows)]
    return pd.DataFrame(
        {
            "open": [c * 0.999 for c in close],
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "adj_close": close,
            "volume": [1000.0 + i for i in range(rows)],
        },
        index=idx,
    )


def _dirty_df(rows: int = 130, *, freq: str = "1h") -> pd.DataFrame:
    df = _clean_df(rows, freq=freq)
    ts = df.index[10]
    df.loc[ts, "open"] = 10.0
    df.loc[ts, "high"] = 10.0
    df.loc[ts, "low"] = 1.0
    df.loc[ts, "close"] = 1.0
    return df


def _run(monkeypatch, tmp_path, df, extra_args: list[str] | None = None, coin: str = "HYPE") -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    seen_networks = []

    def fake_loader(**kwargs):
        seen_networks.append(kwargs["network"])
        return df.copy()

    monkeypatch.setattr(run_overnight_research, "load_historical_candles", fake_loader)
    args = [
        "--coin", coin,
        "--base-config", CONFIG,
        "--start", "2026-01-01",
        "--end", "2026-02-01",
        "--max-grid-runs", "2",
        "--skip-walk-forward",
    ]
    if extra_args:
        args.extend(extra_args)
    assert run_overnight_research.main(args) == 0
    dirs = sorted((tmp_path / "reports/research" / coin.lower()).glob("*"))
    assert dirs
    assert seen_networks
    assert set(seen_networks) == {"mainnet"}
    return dirs[-1]


def test_pipeline_creates_output_directory_and_final_reports(monkeypatch, tmp_path):
    out_dir = _run(monkeypatch, tmp_path, _clean_df())

    assert (out_dir / "overnight_research_summary.json").exists()
    assert (out_dir / "overnight_research_report.md").exists()
    assert (out_dir / "research_recommendation.json").exists()
    assert (out_dir / "history_quality.json").exists()
    assert (out_dir / "baseline.json").exists()
    assert (out_dir / "timeframe_comparison.csv").exists()
    assert (out_dir / "signal_validation.json").exists()
    summary = json.loads((out_dir / "overnight_research_summary.json").read_text())
    assert summary["execution_network"] == "testnet"
    assert summary["historical_data_network"] == "mainnet"


def test_data_quality_stage_stops_pipeline_by_default_on_dirty_data(monkeypatch, tmp_path):
    out_dir = _run(monkeypatch, tmp_path, _dirty_df())
    summary = json.loads((out_dir / "overnight_research_summary.json").read_text())

    assert summary["recommendation"]["verdict"] == "RESEARCH_DATA_DIRTY"
    assert summary["baseline"] is None
    assert not (out_dir / "parameter_sweep.csv").exists()


def test_allow_dirty_data_continues_and_sweep_respects_max_grid_runs(monkeypatch, tmp_path):
    out_dir = _run(monkeypatch, tmp_path, _dirty_df(), ["--allow-dirty-data"])
    sweep = json.loads((out_dir / "parameter_sweep.json").read_text())

    assert sweep["status"] == "COMPLETE"
    assert len(sweep["runs"]) == 2


def test_auto_clean_start_records_selected_clean_start_date(monkeypatch, tmp_path):
    out_dir = _run(monkeypatch, tmp_path, _dirty_df(), ["--auto-clean-start", "--skip-sweep"])
    summary = json.loads((out_dir / "overnight_research_summary.json").read_text())

    assert summary["auto_clean_start_used"] is True
    assert summary["selected_start"] != "2026-01-01"
    assert summary["baseline"]["clean_start_date"] == summary["selected_start"]


def test_zero_trade_timeframe_recommendation_is_inconclusive():
    rows = [
        {"interval": "1h", "trade_count": 2, "time_in_market_pct": 20, "excess_return_vs_buy_hold_pct": 1, "max_drawdown_pct": -5},
        {"interval": "4h", "trade_count": 0, "time_in_market_pct": 0, "excess_return_vs_buy_hold_pct": 5, "max_drawdown_pct": 0},
    ]

    recommendation, reason = run_overnight_research._timeframe_recommend(rows, dirty_allowed=True)

    assert recommendation == "inconclusive"
    assert reason == "inconclusive_zero_trade_timeframe"


def test_signal_validation_cleanly_skips_when_history_is_short(tmp_path):
    df = _clean_df(50)
    report = run_overnight_research.audit_ohlcv_quality(df, interval="1h")

    payload = run_overnight_research.run_signal_validation_stage(df=df, quality_report=report, out_dir=tmp_path)

    assert payload["status"] == "SKIPPED"
    assert (tmp_path / "signal_validation.json").exists()
    assert (tmp_path / "signal_validation.csv").exists()


def test_parameter_sweep_skips_when_data_dirty_without_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    cfg = run_overnight_research.load_config(CONFIG)

    payload = run_overnight_research.run_parameter_sweep_stage(
        df=_clean_df(),
        config=cfg,
        coin="HYPE",
        interval="1h",
        out_dir=tmp_path,
        max_grid_runs=2,
        allowed=False,
        skip=False,
        quality_summary={},
    )

    assert payload["status"] == "SKIPPED"
    assert payload["reason"] == "dirty_data_without_override"


def test_walk_forward_skipped_when_insufficient_data(tmp_path):
    payload = run_overnight_research.run_walk_forward_stage(df=_clean_df(100), out_dir=tmp_path, skip=False)

    assert payload["status"] == "SKIPPED"
    assert payload["reason"] == "insufficient_clean_history_for_30d_train_7d_test"


def test_future_ticker_argument_accepted_structurally(monkeypatch, tmp_path):
    out_dir = _run(monkeypatch, tmp_path, _clean_df(), ["--skip-sweep"], coin="NVDA")
    summary = json.loads((out_dir / "overnight_research_summary.json").read_text())

    assert summary["coin"] == "NVDA"


def test_live_broker_and_telegram_are_not_imported():
    names = set(run_overnight_research.__dict__)

    assert "TelegramNotifier" not in names
    assert "HyperliquidBroker" not in names
    assert "execute_hl" not in names
