"""
Phase 4 tests (Section 25.4).

Required tests:
  1. fetch_ohlcv returns correct columns
  2. trailing stop updated BEFORE decision (not after)
  3. no look-ahead: indicators at bar i only see bars 0..i
  4. gap-down fill executes at next-bar open (not at stop price)
  5. all 20 metrics present in output
  6. backtest completes on 200-bar synthetic data without error
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import List
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.backtest.backtester import BacktestRecord, run_backtest
from src.backtest.metrics import REQUIRED_METRIC_KEYS, compute_metrics
from src.core.models import ActionType, BotState, ThesisState
from tests._helpers import base_config, make_lot, make_state


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_ohlcv(n: int, base_price: float = 100.0, seed: int = 42) -> pd.DataFrame:
    """
    Build a synthetic OHLCV DataFrame of n bars.
    Uses a gentle upward drift so MAs and trend filters are broadly satisfied
    after the warm-up window.
    """
    rng = np.random.default_rng(seed)
    closes = base_price + np.cumsum(rng.normal(0.3, 1.0, n))
    closes = np.maximum(closes, base_price * 0.5)          # keep positive
    opens  = closes - rng.uniform(0.0, 1.0, n)
    highs  = closes + rng.uniform(0.1, 2.0, n)
    lows   = np.minimum(opens, closes) - rng.uniform(0.0, 1.0, n)
    lows   = np.maximum(lows, 1.0)
    volumes = rng.integers(1_000_000, 3_000_000, n).astype(float)
    idx = pd.date_range("2024-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open":      opens,
            "high":      highs,
            "low":       lows,
            "close":     closes,
            "adj_close": closes,
            "volume":    volumes,
        },
        index=idx,
    )


def _minimal_config():
    """Config with required_history_days=50 so 200-bar synthetic test is quick."""
    cfg = base_config()
    cfg.data["required_history_days"] = 50
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# 1. fetch_ohlcv returns correct columns
# ─────────────────────────────────────────────────────────────────────────────

def test_fetch_ohlcv_returns_correct_columns():
    """
    fetch_ohlcv must return a DataFrame with exactly the 6 required columns
    (open, high, low, close, adj_close, volume) and a DatetimeIndex.
    We mock yfinance.download to avoid a network call in unit tests.
    """
    import pandas as pd
    from src.data.market_data import fetch_ohlcv

    # Build a minimal yfinance-style response
    idx = pd.date_range("2025-01-02", periods=5, freq="B")
    mock_df = pd.DataFrame(
        {
            "Open":      [100.0] * 5,
            "High":      [105.0] * 5,
            "Low":       [98.0]  * 5,
            "Close":     [102.0] * 5,
            "Adj Close": [102.0] * 5,
            "Volume":    [1_000_000] * 5,
        },
        index=idx,
    )

    with patch("yfinance.download", return_value=mock_df):
        df = fetch_ohlcv("MU", "2025-01-02", "2025-01-10")

    assert set(df.columns) == {"open", "high", "low", "close", "adj_close", "volume"}
    assert len(df) == 5
    assert hasattr(df.index, "dtype")   # DatetimeIndex


# ─────────────────────────────────────────────────────────────────────────────
# 2. Trailing stop updated BEFORE decision (not after)
# ─────────────────────────────────────────────────────────────────────────────

def test_trailing_stop_updated_before_decision():
    """
    Per Section 21.3 Step 1, the backtester must update state.trailing_stop_price
    from bar T's HIGH BEFORE calling engine_run.

    We spy on engine_run to capture state.trailing_stop_price at call time.
    At a bar with a very high HIGH, the trailing stop captured by the spy must
    already reflect that HIGH — not the pre-bar value.

    Setup:
      - initial trailing_stop = 88, highest_price_since_entry = 100
      - Craft bar at decision_start+1 with HIGH = 200
      - Expected candidate stop = 200 * (1 - 0.12) = 176
      - Spy should see trailing_stop >= 176 when engine_run is called for that bar
    """
    from src.core.decision_engine import run as real_engine

    # Need enough bars so ma50 is valid (requires 50 bars of history).
    # required_history_days=55 → first decision at bar 54 (i=54), df.iloc[:55].
    n = 100
    cfg = _minimal_config()
    cfg.data["required_history_days"] = 55
    df = _make_ohlcv(n, base_price=100.0)

    # Craft bar 56 (two bars after the first decision at bar 54) with a huge HIGH.
    target_bar = 56
    df.iloc[target_bar, df.columns.get_loc("high")]      = 200.0
    df.iloc[target_bar, df.columns.get_loc("adj_close")] = 100.0  # close not relevant here

    seeded = ThesisState(
        symbol="MU",
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 90.0, 10),
        avg_entry_price=90.0,
        current_position_qty=10,
        highest_price_since_entry=100.0,
        trailing_stop_price=88.0,
        initial_stop_price=40.0,
    )

    captured: list = []

    def spy_engine(state, indicators, config):
        captured.append({
            "high": indicators.high,
            "trailing_stop": state.trailing_stop_price,
            "highest": state.highest_price_since_entry,
        })
        return real_engine(state, indicators, config)

    with patch("src.backtest.backtester.engine_run", side_effect=spy_engine):
        run_backtest(df, cfg, initial_state=seeded)

    # Find the capture corresponding to target_bar (HIGH=200).
    target_caps = [c for c in captured if c["high"] is not None and c["high"] >= 199.9]
    assert target_caps, "Spy never saw a bar with high≈200; check target_bar index."

    cap = target_caps[0]
    # If the trailing stop was updated from HIGH=200 before engine_run was called,
    # trailing_stop must be ≥ 200 * (1 - 0.12) = 176.
    expected_min = 200.0 * (1 - 0.12)
    assert cap["trailing_stop"] >= expected_min - 1e-6, (
        f"Trailing stop was {cap['trailing_stop']:.4f} when engine_run was called; "
        f"expected >= {expected_min:.4f} (updated from bar HIGH=200 before decision)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. No look-ahead: indicators at bar i only see bars 0..i
# ─────────────────────────────────────────────────────────────────────────────

def test_no_lookahead_indicators_only_see_history_up_to_bar_i():
    """
    calc_indicators called at bar i must receive df.iloc[:i+1] and NOT df.iloc[:i+2].

    We spy on calc_indicators and record the length of the slice passed at each call.
    The slice length at call k must equal k+1 (1-indexed from required_history start).
    """
    from src.data.indicators import calc_indicators as real_calc
    cfg = _minimal_config()
    cfg.data["required_history_days"] = 5

    df = _make_ohlcv(15)
    slice_lengths: List[int] = []

    def spy_calc(slice_df, cfg_arg):
        slice_lengths.append(len(slice_df))
        return real_calc(slice_df, cfg_arg)

    with patch("src.backtest.backtester.calc_indicators", side_effect=spy_calc):
        run_backtest(df, cfg)

    # Slice lengths must be strictly increasing (each bar adds one row).
    for a, b in zip(slice_lengths, slice_lengths[1:]):
        assert b == a + 1, (
            f"Slice lengths should increase by exactly 1 each bar; got {a} then {b}"
        )

    # The first slice should correspond to bar index required_history_days-1 = 4,
    # so its length is 5 (indices 0..4).
    assert slice_lengths[0] == cfg.data["required_history_days"]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Gap-down fill executes at next-bar open (not at stop price)
# ─────────────────────────────────────────────────────────────────────────────

def test_gap_down_fill_at_next_bar_open_not_stop_price():
    """
    Section 21.3 gap risk: if bar T+1 opens below trailing_stop_price, the fill
    still executes at bar T+1 open — NOT at the stop price.

    Setup:
      - initial trailing_stop = 88, initial_stop = 40
      - Craft decision bar (index 55, one past first decision at 54) so that:
          adj_close = 80  (below trailing_stop=88 → trailing stop fires)
          high      = 92  (slightly above old stop, so stop stays near 88)
      - Craft the NEXT bar (index 56) with open = 70 (gap below stop)
      - With max_slippage_bps=0: fill_price == 70.0 exactly

    Expected:
      - A SELL_TRAILING_STOP record with fill_price ≈ 70
      - gap_loss = True on that record
    """
    n = 100
    cfg = _minimal_config()
    cfg.data["required_history_days"] = 55
    cfg.execution["max_slippage_bps"] = 0   # deterministic fills

    df = _make_ohlcv(n, base_price=100.0)

    # Bar 55: close below trailing stop → decision engine fires SELL_TRAILING_STOP.
    # HIGH must stay low enough that the raised stop stays above 80.
    trigger_bar  = 55   # i=55 → decision bar; fill at bar 56
    gap_open_bar = 56

    df.iloc[trigger_bar, df.columns.get_loc("adj_close")] = 80.0
    df.iloc[trigger_bar, df.columns.get_loc("close")]     = 80.0
    df.iloc[trigger_bar, df.columns.get_loc("high")]      = 92.0   # new high → stop ~ 92*(1-0.12)=81 > 80
    df.iloc[gap_open_bar, df.columns.get_loc("open")]     = 70.0   # gap down below stop

    seeded = ThesisState(
        symbol="MU",
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 90.0, 10),
        avg_entry_price=90.0,
        current_position_qty=10,
        highest_price_since_entry=90.0,   # start with high=90 so stop ~ 90*0.88=79.2 < 80
        trailing_stop_price=79.0,         # below adj_close=80 → won't fire on prior bars
        initial_stop_price=40.0,
    )
    # After bar 55 HIGH=92: new stop = max(79, 92*(1-0.12)) = max(79, 80.96) = 80.96
    # adj_close=80 < 80.96 → trailing stop fires
    # Fill at bar 56 open=70; 70 < 80.96 → gap_loss=True

    result = run_backtest(df, cfg, initial_state=seeded)

    stop_recs = [
        r for r in result.records
        if r.action in (ActionType.SELL_TRAILING_STOP, ActionType.SELL_STOP,
                        ActionType.EXIT_ALL)
        and r.fill_price is not None
    ]

    assert stop_recs, (
        "Expected a stop record with a fill price. "
        f"Actions seen: {[r.action for r in result.records if r.action != ActionType.NO_ACTION]}"
    )
    rec = stop_recs[0]
    # With slippage=0, fill_price should equal bar 56 open = 70.0.
    assert abs(rec.fill_price - 70.0) < 1.0, (
        f"Expected fill near gap open 70.0, got {rec.fill_price:.4f}. "
        "Fill must execute at next-bar open, not at the stop price."
    )
    assert rec.gap_loss is True, "gap_loss flag must be True when open gaps below stop."


# ─────────────────────────────────────────────────────────────────────────────
# 5. All 19 metrics present in output
# ─────────────────────────────────────────────────────────────────────────────

def test_all_20_metrics_present():
    cfg = _minimal_config()
    df = _make_ohlcv(200)
    result = run_backtest(df, cfg)
    assert set(result.metrics.keys()) == REQUIRED_METRIC_KEYS, (
        f"Missing keys: {REQUIRED_METRIC_KEYS - set(result.metrics.keys())}\n"
        f"Extra keys: {set(result.metrics.keys()) - REQUIRED_METRIC_KEYS}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Backtest completes on 200-bar synthetic data without error
# ─────────────────────────────────────────────────────────────────────────────

def test_backtest_completes_on_200_bars_without_error():
    cfg = _minimal_config()
    df = _make_ohlcv(200)
    result = run_backtest(df, cfg)
    # Must return a BacktestResult with records and a metrics dict.
    assert result.records is not None
    assert len(result.records) > 0
    assert isinstance(result.metrics, dict)
    assert len(result.records) == len(df) - 1   # n-1 decision bars


# ─────────────────────────────────────────────────────────────────────────────
# 7. compute_metrics returns all 19 keys on empty record list
# ─────────────────────────────────────────────────────────────────────────────

def test_compute_metrics_handles_empty_records():
    cfg = base_config()
    metrics = compute_metrics([], cfg)
    assert set(metrics.keys()) == REQUIRED_METRIC_KEYS
    assert metrics["total_return"] == 0.0
    assert metrics["number_of_entries"] == 0
    assert metrics["gap_loss_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 8. Metrics correctly count trade-type actions
# ─────────────────────────────────────────────────────────────────────────────

def test_metrics_count_trade_actions_correctly():
    cfg = base_config()
    equity = cfg.capital["starting_equity"]

    def _rec(action):
        return BacktestRecord(
            bar_date=date(2025, 1, 2),
            adj_close=100.0,
            open_price=99.0,
            qty=10,
            avg_entry_price=90.0,
            fill_price=100.0,
            fill_qty=(-10 if action in (
                ActionType.SELL_TAKE_PROFIT, ActionType.SELL_STOP,
                ActionType.SELL_TRAILING_STOP, ActionType.EXIT_ALL,
                ActionType.SELL_REDUCE_ADDON, ActionType.SELL_REDUCE_BASE,
                ActionType.SELL_EVENT_DERISKING,
            ) else 10),
            action=action,
            state_name="BASE_LONG",
            in_event_window=False,
            exposure_pct=0.10,
            trailing_stop_price=None,
            gap_loss=False,
        )

    records = [
        _rec(ActionType.BUY_STARTER),
        _rec(ActionType.BUY_ADDON),
        _rec(ActionType.BUY_ADDON),
        _rec(ActionType.SELL_REDUCE_ADDON),
        _rec(ActionType.SELL_TAKE_PROFIT),
        _rec(ActionType.SELL_STOP),
    ]
    metrics = compute_metrics(records, cfg)
    assert metrics["number_of_entries"] == 1
    assert metrics["number_of_adds"] == 2
    assert metrics["number_of_reduces"] == 1
    assert metrics["number_of_take_profits"] == 1
    assert metrics["number_of_stops"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 9. charts.py raises ImportError clearly when matplotlib is missing
# ─────────────────────────────────────────────────────────────────────────────

def test_charts_raises_import_error_when_matplotlib_missing():
    from src.backtest.charts import generate_charts
    cfg = base_config()
    # Create a minimal BacktestResult-like mock
    mock_result = MagicMock()
    mock_result.records = []

    with patch("src.backtest.charts._require_matplotlib",
               side_effect=ImportError("matplotlib not installed")):
        with pytest.raises(ImportError, match="matplotlib"):
            generate_charts(mock_result, cfg, "/tmp/charts")
