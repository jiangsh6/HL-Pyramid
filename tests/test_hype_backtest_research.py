from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from scripts import compare_hype_timeframes, sweep_hype_parameters
from src.backtest.backtest_metrics import compute_backtest_metrics
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


def _crash_df(rows: int = 90) -> pd.DataFrame:
    df = _df(rows)
    for i in range(60, rows):
        value = max(1.0, 11.8 - (i - 59) * 0.55)
        df.iloc[i, df.columns.get_loc("open")] = value
        df.iloc[i, df.columns.get_loc("high")] = value * 1.01
        df.iloc[i, df.columns.get_loc("low")] = value * 0.98
        df.iloc[i, df.columns.get_loc("close")] = value
        df.iloc[i, df.columns.get_loc("adj_close")] = value
    return df


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


def test_full_exit_order_equity_after_does_not_include_stale_unrealized(monkeypatch, tmp_path):
    cfg = _cfg(monkeypatch)
    result = run_research_backtest(
        df=_crash_df(),
        config=cfg,
        coin="HYPE",
        interval="1h",
        run_dir=tmp_path,
        run_id="crash_test",
        slippage_bps=0.0,
    )
    sell = next(order for order in result.orders if order["side"] == "sell")

    assert sell["account_equity_after"] == sell["account_equity_before"]


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


def test_simple_trade_return_pct_metrics(monkeypatch):
    cfg = _cfg(monkeypatch)
    equity_curve = pd.DataFrame({
        "equity": [900.0, 910.0, 920.0],
        "cash_equity": [900.0, 910.0, 920.0],
        "unrealized_pnl": [0.0, 0.0, 0.0],
        "close": [100.0, 105.0, 110.0],
        "position_qty": [0.0, 0.0, 0.0],
        "exposure_pct": [0.0, 0.0, 0.0],
        "risk_budget_utilization": [0.0, 0.0, 0.0],
        "add_count": [0, 0, 0],
    })
    orders = [
        {"side": "sell", "action": "sell_stop", "realized_pnl": 10.0, "return_pct": 10.0, "cost_basis": 100.0},
        {"side": "sell", "action": "sell_stop", "realized_pnl": -5.0, "return_pct": -5.0, "cost_basis": 100.0},
    ]

    metrics = compute_backtest_metrics(
        equity_curve=equity_curve,
        orders=orders,
        decisions=[],
        starting_equity=900.0,
        first_close=100.0,
        last_close=110.0,
        interval_hours=1.0,
        config=cfg,
    )

    assert metrics["avg_win_pct"] == 10.0
    assert metrics["avg_loss_pct"] == -5.0
    assert metrics["worst_trade_pct"] == -5.0
    assert round(metrics["total_return_pct"], 6) == round((920.0 / 900.0 - 1.0) * 100.0, 6)


def test_no_leverage_loss_bounded_and_accounting_invariants(monkeypatch):
    cfg = _cfg(monkeypatch)
    equity_curve = pd.DataFrame({
        "equity": [900.0, 850.0],
        "cash_equity": [900.0, 860.0],
        "unrealized_pnl": [0.0, -10.0],
        "close": [100.0, 90.0],
        "position_qty": [1.0, 1.0],
        "exposure_pct": [0.2, 0.2],
        "risk_budget_utilization": [0.1, 0.1],
        "add_count": [0, 0],
    })
    orders = [
        {"side": "sell", "action": "sell_stop", "realized_pnl": -40.0, "return_pct": -40.0, "cost_basis": 100.0},
    ]

    metrics = compute_backtest_metrics(
        equity_curve=equity_curve,
        orders=orders,
        decisions=[],
        starting_equity=900.0,
        first_close=100.0,
        last_close=90.0,
        interval_hours=1.0,
        config=cfg,
    )

    assert metrics["no_leverage_loss_bound_ok"] is True
    assert metrics["account_equity_non_negative"] is True
    assert metrics["accounting_reconciled"] is True
    assert metrics["max_exposure_within_config"] is True


def test_zero_trade_timeframe_recommendation_is_inconclusive():
    rows = [
        {"interval": "1h", "trade_count": 2, "time_in_market_pct": 50, "excess_return_vs_buy_hold_pct": -5, "max_drawdown_pct": -10},
        {"interval": "4h", "trade_count": 0, "time_in_market_pct": 0, "excess_return_vs_buy_hold_pct": 0, "max_drawdown_pct": 0},
    ]

    assert compare_hype_timeframes._recommend(rows) == "inconclusive"
    assert compare_hype_timeframes._recommendation_reason(rows, "inconclusive") == "inconclusive_zero_trade_timeframe"
