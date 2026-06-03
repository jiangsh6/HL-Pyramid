from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from scripts import calibrate_hype_strategy, run_overnight_research
from src.core.config_loader import load_config


TESTNET_WALLET = "0x1111111111111111111111111111111111111111"
CONFIG = "/Users/shujiajiang/Crypto Dev/HL Primiding/config/hype_candidate_normal_v2.yaml"


def _df(rows: int = 140) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=rows, freq="1h", tz="UTC")
    close = [10.0 + i * 0.04 for i in range(rows)]
    return pd.DataFrame(
        {
            "open": [c * 0.999 for c in close],
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "adj_close": close,
            "volume": [1000.0 + i * 2 for i in range(rows)],
        },
        index=idx,
    )


def test_calibration_script_runs_on_short_fixture(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    seen_networks = []

    def fake_loader(**kwargs):
        seen_networks.append(kwargs["network"])
        return _df()

    monkeypatch.setattr(calibrate_hype_strategy, "load_historical_candles", fake_loader)

    status = calibrate_hype_strategy.main([
        "--coin", "HYPE",
        "--base-config", CONFIG,
        "--start", "2026-01-01",
        "--end", "2026-02-01",
        "--max-runs", "2",
    ])

    assert status == 0
    assert seen_networks == ["mainnet"]
    dirs = list((tmp_path / "reports/research/hype").glob("calibration_*"))
    assert dirs
    assert (dirs[0] / "calibration_summary.csv").exists()
    assert (dirs[0] / "calibration_report.md").exists()


def test_generated_loose_config_preserves_required_fields(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    monkeypatch.setattr(calibrate_hype_strategy, "load_historical_candles", lambda **kwargs: _df())

    calibrate_hype_strategy.main([
        "--coin", "HYPE",
        "--base-config", CONFIG,
        "--start", "2026-01-01",
        "--end", "2026-02-01",
        "--max-runs", "1",
    ])

    cfg = load_config("config/hype_candidate_loose_v1.yaml")
    assert cfg.bot["name"] == "hype_candidate_loose_v1"
    assert cfg.hl["network"] == "testnet"
    assert cfg.data["network"] == "mainnet"
    assert cfg.symbol["ticker"] == "HYPE"
    assert cfg.entry["starter"]["enabled"] is True


def test_blocker_frequency_summary_is_written(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    monkeypatch.setattr(calibrate_hype_strategy, "load_historical_candles", lambda **kwargs: _df())

    calibrate_hype_strategy.main([
        "--coin", "HYPE",
        "--base-config", CONFIG,
        "--start", "2026-01-01",
        "--end", "2026-02-01",
        "--max-runs", "1",
    ])

    summary_path = next((tmp_path / "reports/research/hype").glob("calibration_*/calibration_summary.csv"))
    rows = list(csv.DictReader(summary_path.open()))
    assert rows
    assert "top_blockers_json" in rows[0]
    assert "readiness_json" in rows[0]


def test_overnight_verdict_logic_not_weakened_globally():
    verdict = run_overnight_research.final_verdict(
        data_clean=True,
        allow_dirty=False,
        signal_payload={"status": "COMPLETE", "rows": [{"average_forward_return_24h": 1.0}]},
        timeframe_payload={"recommendation": "inconclusive"},
    )

    assert verdict == "RESEARCH_NEEDS_MORE_DATA"


def test_calibration_path_does_not_import_live_broker_or_telegram():
    names = set(calibrate_hype_strategy.__dict__)

    assert "TelegramNotifier" not in names
    assert "HyperliquidBroker" not in names
    assert "execute_hl" not in names
