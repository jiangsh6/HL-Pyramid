from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts import compare_hype_timeframes, sweep_hype_parameters
from src.backtest.historical_loader import normalize_ohlcv
from src.backtest.pyramiding_backtester import run_research_backtest
from src.core.config_loader import load_config


TESTNET_WALLET = "0x1111111111111111111111111111111111111111"


def _df(rows: int = 90, *, freq: str = "1h") -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=rows, freq=freq, tz="UTC")
    close = [10 + i * 0.03 for i in range(rows)]
    open_ = [c * 0.999 for c in close]
    return pd.DataFrame(
        {
            "open": open_,
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


def test_historical_loader_normalizes_required_ohlcv_fields():
    raw = _df().drop(columns=["adj_close"])

    out = normalize_ohlcv(raw)

    assert list(out.columns) == ["open", "high", "low", "close", "adj_close", "volume"]
    assert out["adj_close"].equals(out["close"])


def test_backtest_next_bar_open_fill_and_outputs(monkeypatch, tmp_path):
    cfg = _cfg(monkeypatch)
    df = _df()
    result = run_research_backtest(
        df=df,
        config=cfg,
        coin="HYPE",
        interval="1h",
        run_dir=tmp_path,
        run_id="test_run",
        slippage_bps=0.0,
    )

    assert result.orders
    first_order = result.orders[0]
    fill_ts = pd.Timestamp(first_order["timestamp"])
    fill_idx = df.index.get_loc(fill_ts)
    assert first_order["fill_price"] == df.iloc[fill_idx]["open"]
    assert (tmp_path / "orders.csv").exists()
    assert (tmp_path / "positions.csv").exists()
    assert (tmp_path / "decisions.csv").exists()
    assert (tmp_path / "signal_diagnostics.csv").exists()
    assert (tmp_path / "equity_curve.csv").exists()
    assert (tmp_path / "backtest_summary.json").exists()
    assert json.loads((tmp_path / "backtest_summary.json").read_text())["strategy_scope"] == "starter_add_stop_only"


def test_no_trade_on_nan_indicator_rows(monkeypatch, tmp_path):
    cfg = _cfg(monkeypatch)
    result = run_research_backtest(
        df=_df(55),
        config=cfg,
        coin="HYPE",
        interval="1h",
        run_dir=tmp_path,
        run_id="nan_test",
    )

    assert any(row["reason"] == "insufficient_indicator_history" for row in result.decisions)


def test_state_updates_after_simulated_fill(monkeypatch, tmp_path):
    cfg = _cfg(monkeypatch)
    result = run_research_backtest(
        df=_df(),
        config=cfg,
        coin="HYPE",
        interval="1h",
        run_dir=tmp_path,
        run_id="state_test",
    )

    assert result.final_state.current_position_qty > 0
    assert result.final_state.base_lot is not None


def test_compare_timeframes_produces_output(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source_cfg = Path("/Users/shujiajiang/Crypto Dev/HL Primiding/config/hype_candidate_normal_v2.yaml")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(source_cfg.read_text())
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    monkeypatch.setattr(compare_hype_timeframes, "load_historical_candles", lambda **kwargs: _df(freq=kwargs["interval"]))

    status = compare_hype_timeframes.main([
        "--config", str(cfg_path),
        "--start", "2026-01-01",
        "--end", "2026-02-01",
    ])

    assert status == 0
    assert list((tmp_path / "reports/backtests/hype").glob("timeframe_comparison_*.csv"))
    assert list((tmp_path / "reports/backtests/hype").glob("timeframe_comparison_*.md"))


def test_parameter_sweep_produces_output(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source_cfg = Path("/Users/shujiajiang/Crypto Dev/HL Primiding/config/hype_candidate_normal_v2.yaml")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(source_cfg.read_text())
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    monkeypatch.setattr(sweep_hype_parameters, "load_historical_candles", lambda **kwargs: _df())

    status = sweep_hype_parameters.main([
        "--base-config", str(cfg_path),
        "--interval", "1h",
        "--start", "2026-01-01",
        "--end", "2026-02-01",
        "--max-runs", "2",
    ])

    assert status == 0
    assert list((tmp_path / "reports/backtests/hype").glob("parameter_sweep_*.csv"))
    assert list((tmp_path / "reports/backtests/hype").glob("parameter_sweep_*.json"))
    assert list((tmp_path / "reports/backtests/hype").glob("parameter_sweep_*.md"))


def test_backtest_research_does_not_import_live_execution_or_telegram():
    import src.backtest.pyramiding_backtester as module

    names = set(module.__dict__)
    assert "execute_hl" not in names
    assert "TelegramNotifier" not in names


def test_future_ticker_argument_is_structurally_accepted(monkeypatch, tmp_path):
    cfg = _cfg(monkeypatch)
    result = run_research_backtest(
        df=_df(),
        config=cfg,
        coin="NVDA",
        interval="1h",
        run_dir=tmp_path,
        run_id="future_ticker",
    )

    assert result.final_state.symbol == "NVDA"
