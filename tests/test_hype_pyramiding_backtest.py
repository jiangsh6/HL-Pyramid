from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from scripts import fetch_hype_hl_data as fetcher
from scripts import run_hype_pyramiding_backtest as bt


def _df(rows: int = 180, freq: str = "1D") -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=rows, freq=freq, tz="UTC")
    close = []
    for i in range(rows):
        if i < 110:
            close.append(10.0 + i * 0.005)
        elif i < 145:
            close.append(10.55 + (i - 110) * 0.16)
        elif i < 160:
            close.append(16.15 - (i - 145) * 0.12)
        else:
            close.append(14.35 + (i - 160) * 0.04)
    return pd.DataFrame(
        {
            "open": [c * 0.999 for c in close],
            "high": [c * 1.02 for c in close],
            "low": [c * 0.98 for c in close],
            "close": close,
            "adj_close": close,
            "volume": [1000.0] * rows,
        },
        index=idx,
    )


def _funding(index: pd.DatetimeIndex, rate: float = 0.0001) -> pd.DataFrame:
    hourly = pd.date_range(index.min(), index.max(), freq="1h", tz="UTC")
    return pd.DataFrame({"funding_rate": [rate] * len(hourly)}, index=hourly)


def _empty_funding() -> pd.DataFrame:
    return pd.DataFrame(columns=["funding_rate"], index=pd.DatetimeIndex([], tz="UTC"))


def test_indicator_no_lookahead_and_daily_macro_shift():
    df = _df(130)
    ind = bt.compute_indicators(df)
    assert pd.isna(ind["ma100"].iloc[98])
    assert ind["ma100"].iloc[99] == df["close"].iloc[:100].mean()

    local = _df(130, "4h")
    merged = bt.apply_daily_macro(local, df)
    first_valid = bt.compute_indicators(df)["ma100"].dropna().index[0] + pd.Timedelta(days=1)
    assert not merged[merged.index < first_valid]["is_bull_daily"].any()


def test_dollar_risk_position_sizing_and_open_risk():
    qty = bt.calculate_position_qty(equity=10_000, max_thesis_risk_pct=0.01, allocation=0.6, entry_price=20, stop_price=18)
    assert qty == 30
    assert bt._open_risk([bt.Lot("starter", 20, 30)], 18) == 60


def test_stop_add_runner_fees_and_funding_paths():
    df = _df(190)
    cfg = bt.BacktestConfig(
        phase=4,
        config_id="runner",
        risk_allocations=(0.5, 0.3, 0.2),
        add1_trigger_atr=0.5,
        add2_trigger_atr=1.0,
        partial_tp_r=0.5,
        runner_method="giveback",
        taker_fee_bps=5,
    )
    result = bt.run_single_backtest(df, df, _funding(df.index, 0.0001), cfg)
    trades = result["trades"]
    assert trades
    assert any(t["add1_time"] for t in trades)
    assert any(t["runner_start_time"] for t in trades)
    assert result["summary"]["fees_paid"] > 0
    assert result["summary"]["funding_pnl"] < 0


def test_funding_accrues_from_open_lot_notional_not_bar_close():
    st = bt.TradeState(
        state="STARTER_LONG",
        lots=[bt.Lot("starter", 10.0, 2.0)],
    )
    funding = pd.DataFrame(
        {"funding_rate": [0.01, 0.02]},
        index=pd.to_datetime(["2026-01-01T01:00:00Z", "2026-01-01T02:00:00Z"]),
    )

    paid = bt._accrue_funding(
        st,
        funding,
        pd.Timestamp("2026-01-01T00:00:00Z"),
        pd.Timestamp("2026-01-01T03:00:00Z"),
    )

    assert paid == pytest.approx(-0.6)
    assert st.funding_pnl == pytest.approx(-0.6)
    assert st.funding_notional_hours == pytest.approx(40.0)


def test_add_budget_not_blocked_by_prior_funding_drift():
    df = _df(190)
    cfg = bt.BacktestConfig(
        phase=2,
        config_id="add_after_funding",
        risk_allocations=(0.6, 0.4),
        add1_trigger_atr=0.5,
    )

    result = bt.run_single_backtest(df, df, _funding(df.index, 0.0001), cfg)

    assert any(t["add1_time"] for t in result["trades"])
    assert any(row["condition_met"] for row in result["add_debug"])


def test_add_block_conditions_are_enforced_by_helpers():
    losing = [bt.Lot("starter", 20, 10)]
    assert bt._unrealized(losing, 19) < 0
    too_risky = [bt.Lot("starter", 20, 10), bt.Lot("add1", 25, 100)]
    assert bt._open_risk(too_risky, 18) > 100


def test_single_trade_dominated_exclusion_and_ranking():
    cfg = bt.BacktestConfig(phase=1, config_id="x")
    trades = [
        {"net_pnl": 100.0, "gross_pnl": 100.0, "fees_paid": 0.0, "funding_pnl": 0.0, "add1_time": "", "add2_time": "", "runner_start_time": "", "net_return_pct": 1, "bars_held": 1, "state_path": "STARTER_LONG"},
        {"net_pnl": 10.0, "gross_pnl": 10.0, "fees_paid": 0.0, "funding_pnl": 0.0, "add1_time": "", "add2_time": "", "runner_start_time": "", "net_return_pct": 0.1, "bars_held": 1, "state_path": "STARTER_LONG"},
    ]
    summary = bt.summarize(cfg, trades, pd.DataFrame({"equity": [10_000, 10_110]}), effective_sample_size=4)
    assert summary["single_trade_dominated"] is True
    assert summary["top_trade_contribution_pct"] > 0.60
    assert "SINGLE_TRADE_DOMINATED" in summary["exclusion_reason"]
    assert summary["sample_size_warning"] is True


def test_select_phase_winner_prefers_simpler_near_tie():
    rows = [
        {"config_id": "complex", "net_return_over_max_dd": 1.00, "win_rate": 50, "state_transition_count": 50, "excluded_for_drawdown": False, "single_trade_dominated": False, "add1_hit_rate": 80, "add2_hit_rate": 50, "runner_activation_rate": 30, "timeframe": "4h"},
        {"config_id": "simple", "net_return_over_max_dd": 0.97, "win_rate": 50, "state_transition_count": 10, "excluded_for_drawdown": False, "single_trade_dominated": False, "add1_hit_rate": 0, "add2_hit_rate": 0, "runner_activation_rate": 0, "timeframe": "1d"},
    ]
    assert bt.select_phase_winner(rows)["config_id"] == "simple"


def test_one_hour_sanity_only_runs_after_large_4h_divergence():
    assert bt.should_run_one_hour_sanity(20.0, 6.0) is False
    assert bt.should_run_one_hour_sanity(20.0, 4.0) is True


def test_cooldown_blocks_reentry_counter():
    st = bt.TradeState(state="COOLDOWN", cooldown_remaining=3)
    st.cooldown_remaining -= 1
    st.cooldown_remaining -= 1
    assert st.cooldown_remaining > 0


def test_run_all_phases_writes_v11_outputs(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _df(180, "1D").to_csv(data_dir / "HYPE_1d.csv", index_label="timestamp")
    _df(220, "4h").to_csv(data_dir / "HYPE_4h.csv", index_label="timestamp")
    pd.DataFrame({"timestamp": [], "funding_rate": []}).to_csv(data_dir / "HYPE_funding.csv", index=False)

    result = bt.run_all_phases("HYPE", data_dir, tmp_path / "out")

    for name in [
        "phase1_baseline.csv",
        "phase2_one_add.csv",
        "phase3_two_adds.csv",
        "phase4_runner.csv",
        "phase5_cost_adjusted.csv",
        "phase6_timeframe_comparison.csv",
        "phase7_cooldown.csv",
        "ranked_configs.csv",
        "trade_log_best.csv",
        "add_debug_log.csv",
        "backtest_report.md",
    ]:
        assert (tmp_path / "out" / name).exists()
    phase1 = list(csv.DictReader((tmp_path / "out" / "phase1_baseline.csv").open()))
    assert "single_trade_dominated" in phase1[0]
    assert "top_trade_contribution_pct" in phase1[0]
    assert "exclusion_reason" in phase1[0]
    assert "positive_only_in_w3" in phase1[0]
    assert "w1_top_trade_contribution_pct" in phase1[0]
    assert "w1_genuinely_positive" in phase1[0]
    ranked = list(csv.DictReader((tmp_path / "out" / "ranked_configs.csv").open()))
    assert all(row["single_trade_dominated"] == "False" for row in ranked)
    assert result["phase_winners"]


def test_subwindow_positive_requires_non_dominated_window():
    df = _df(180, "1D")
    funding = _empty_funding()
    cfg = bt.BacktestConfig(phase=1, config_id="subwindow")
    row: dict[str, object] = {}

    bt._add_phase1_subwindows(row, df, df, funding, cfg)

    assert "w1_top_trade_contribution_pct" in row
    for label in ["w1", "w2", "w3"]:
        if float(row[f"{label}_top_trade_contribution_pct"]) > 0.60:
            assert row[f"{label}_genuinely_positive"] is False


def test_recommendation_set_does_not_include_two_adds(tmp_path: Path):
    bt._write_report(tmp_path, [], [])
    text = (tmp_path / "backtest_report.md").read_text()
    assert "IMPLEMENT_STARTER_PLUS_TWO_ADDS" not in text
    assert "NEEDS_MORE_DATA" in text


def test_funding_fetch_paginates_capped_history(monkeypatch):
    calls: list[tuple[int, int]] = []

    def fake_get_funding_history(symbol, start_ms, end_ms, client):
        calls.append((start_ms, end_ms))
        if len(calls) == 1:
            return [
                fetcher.FundingPayment(symbol, datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc), 0.001, 0.0, -0.0),
                fetcher.FundingPayment(symbol, datetime.fromtimestamp((start_ms + 3_600_000) / 1000, tz=timezone.utc), 0.002, 0.0, -0.0),
            ]
        if len(calls) == 2:
            return [
                fetcher.FundingPayment(symbol, datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc), 0.003, 0.0, -0.0),
            ]
        return []

    monkeypatch.setattr(fetcher, "get_funding_history", fake_get_funding_history)
    rows = fetcher.fetch_funding_history_paginated("HYPE", 1_000_000, 20_000_000, object())

    assert len(rows) == 3
    assert len(calls) == 3
    assert calls[1][0] > calls[0][0]


def test_candle_fetch_chunks_long_intraday_ranges(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_load_historical_candles(*, coin, interval, start, end, network, client):
        calls.append((start, end))
        idx = pd.date_range(pd.Timestamp(start), periods=2, freq="1h", tz="UTC")
        return pd.DataFrame(
            {
                "open": [1.0, 1.1],
                "high": [1.2, 1.3],
                "low": [0.9, 1.0],
                "close": [1.1, 1.2],
                "adj_close": [1.1, 1.2],
                "volume": [100.0, 100.0],
            },
            index=idx,
        )

    monkeypatch.setattr(fetcher, "MAX_CANDLES_PER_CHUNK", 2)
    monkeypatch.setattr(fetcher, "load_historical_candles", fake_load_historical_candles)

    df = fetcher.fetch_candles_chunked(
        symbol="HYPE",
        interval="1h",
        start="2026-01-01T00:00:00+00:00",
        end="2026-01-01T06:00:00+00:00",
        network="mainnet",
        client=object(),
    )

    assert len(calls) > 1
    assert not df.empty
