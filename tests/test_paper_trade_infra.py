from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from scripts import cron_fetch_hype_data as cron_fetch
from scripts import fetch_hype_hl_data as fetch_hype
from scripts import paper_trade_progress


def test_cron_fetch_incremental_append_is_idempotent(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    log_path = tmp_path / "logs" / "data_fetch.log"
    data_dir.mkdir()
    pd.DataFrame(
        {
            "timestamp": ["2026-01-01T00:00:00+00:00"],
            "open": [1.0],
            "high": [1.1],
            "low": [0.9],
            "close": [1.0],
            "adj_close": [1.0],
            "volume": [100.0],
        }
    ).to_csv(data_dir / "HYPE_1d.csv", index=False)
    pd.DataFrame(
        {
            "timestamp": ["2026-01-01T00:00:00+00:00"],
            "funding_rate": [0.001],
            "position_usd": [0.0],
            "payment_usd": [0.0],
        }
    ).to_csv(data_dir / "HYPE_funding.csv", index=False)

    def fake_candles(**kwargs):
        idx = pd.to_datetime(["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"])
        return pd.DataFrame(
            {
                "open": [1.0, 2.0],
                "high": [1.1, 2.1],
                "low": [0.9, 1.9],
                "close": [1.0, 2.0],
                "adj_close": [1.0, 2.0],
                "volume": [100.0, 200.0],
            },
            index=idx,
        )

    def fake_funding(symbol, start_ms, end_ms, client):
        return [
            fetch_hype.FundingPayment(
                coin=symbol,
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                funding_rate=0.001,
                position_usd=0.0,
                payment_usd=0.0,
            ),
            fetch_hype.FundingPayment(
                coin=symbol,
                timestamp=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
                funding_rate=0.002,
                position_usd=0.0,
                payment_usd=0.0,
            ),
        ]

    monkeypatch.setattr(cron_fetch, "fetch_candles_chunked", fake_candles)
    monkeypatch.setattr(cron_fetch, "fetch_funding_history_paginated", fake_funding)

    first = cron_fetch.append_hype_data(data_dir=data_dir, log_path=log_path, intervals=["1d"], end="2026-01-03T00:00:00+00:00", client=object())
    second = cron_fetch.append_hype_data(data_dir=data_dir, log_path=log_path, intervals=["1d"], end="2026-01-03T00:00:00+00:00", client=object())

    assert first["1d"]["added_rows"] == 1
    assert second["1d"]["added_rows"] == 0
    assert len(pd.read_csv(data_dir / "HYPE_1d.csv")) == 2
    assert len(pd.read_csv(data_dir / "HYPE_funding.csv")) == 2
    assert log_path.exists()


def test_paper_trade_progress_handles_empty_logs(tmp_path: Path):
    report = paper_trade_progress.build_progress_report(tmp_path / "paper", tmp_path / "data")

    assert "Paper Trade Progress Report" in report
    assert "Current state: FLAT" in report
    assert "Independent bull episodes observed: 0 / 15 target" in report
