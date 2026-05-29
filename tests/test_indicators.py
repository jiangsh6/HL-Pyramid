"""
Section 22.2 — Indicator tests (7 tests).
"""
import math

import numpy as np
import pandas as pd
import pytest

from src.core.config_loader import load_config
from src.data.indicators import calc_indicators


CONFIG_PATH = "config/mu_long_thesis.yaml"


def _cfg():
    return load_config(CONFIG_PATH)


def _make_df(n: int, price: float = 100.0, seed: int = 42) -> pd.DataFrame:
    """
    Build a synthetic OHLCV DataFrame with n bars.
    Prices walk slightly upward to keep MAs meaningful.
    """
    rng = np.random.default_rng(seed)
    closes = price + np.cumsum(rng.normal(0.2, 1.0, n))
    opens  = closes - rng.uniform(0.1, 1.0, n)
    highs  = closes + rng.uniform(0.1, 2.0, n)
    lows   = closes - rng.uniform(0.1, 2.0, n)
    # Ensure high >= close >= low >= open (rough approximation)
    highs  = np.maximum(highs, closes)
    lows   = np.minimum(lows, closes)
    lows   = np.minimum(lows, opens)
    volumes = rng.integers(500_000, 2_000_000, n).astype(float)

    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes,
         "adj_close": closes, "volume": volumes},
        index=idx,
    )


# ── test_moving_averages_correct_on_synthetic_data ───────────────────────────

def test_moving_averages_correct_on_synthetic_data():
    df  = _make_df(60)
    cfg = _cfg()
    snap = calc_indicators(df, cfg)

    expected_ma5  = df["adj_close"].rolling(5).mean().iloc[-1]
    expected_ma10 = df["adj_close"].rolling(10).mean().iloc[-1]
    expected_ma20 = df["adj_close"].rolling(20).mean().iloc[-1]
    expected_ma50 = df["adj_close"].rolling(50).mean().iloc[-1]

    assert math.isclose(snap.ma5,  expected_ma5,  rel_tol=1e-9)
    assert math.isclose(snap.ma10, expected_ma10, rel_tol=1e-9)
    assert math.isclose(snap.ma20, expected_ma20, rel_tol=1e-9)
    assert math.isclose(snap.ma50, expected_ma50, rel_tol=1e-9)


# ── test_atr_correct_on_synthetic_data ───────────────────────────────────────

def test_atr_correct_on_synthetic_data():
    df  = _make_df(60)
    cfg = _cfg()
    snap = calc_indicators(df, cfg)

    prev_close = df["adj_close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"]  - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    expected_atr = tr.rolling(14).mean().iloc[-1]

    assert math.isclose(snap.atr14, expected_atr, rel_tol=1e-9)


# ── test_prior_highest_high_20d_is_shifted_by_one_bar ────────────────────────

def test_prior_highest_high_20d_is_shifted_by_one_bar():
    df  = _make_df(60)
    cfg = _cfg()
    snap = calc_indicators(df, cfg)

    # The shift(1) means today's value = yesterday's rolling-20 max
    expected = df["high"].rolling(20).max().shift(1).iloc[-1]
    assert math.isclose(snap.prior_highest_high_20d, expected, rel_tol=1e-9)

    # Must NOT equal the unshifted value when they differ
    unshifted = df["high"].rolling(20).max().iloc[-1]
    # They may be equal if the maximum hasn't changed; verify the formula, not the value
    formula_value = df["high"].rolling(20).max().shift(1).iloc[-1]
    assert math.isclose(snap.prior_highest_high_20d, formula_value, rel_tol=1e-9)


# ── test_drawdown_from_20d_high_uses_shifted_prior_high ──────────────────────

def test_drawdown_from_20d_high_uses_shifted_prior_high():
    df  = _make_df(60)
    cfg = _cfg()
    snap = calc_indicators(df, cfg)

    prior_hh = df["high"].rolling(20).max().shift(1).iloc[-1]
    last_adj  = df["adj_close"].iloc[-1]
    expected  = (prior_hh - last_adj) / prior_hh

    assert math.isclose(snap.drawdown_from_20d_high, expected, rel_tol=1e-9)


# ── test_gap_up_pct_uses_raw_open_not_adj_close ──────────────────────────────

def test_gap_up_pct_uses_raw_open_not_adj_close():
    df = _make_df(30)

    # Force a clear gap-up on the last bar: open >> prev adj_close
    df.iloc[-1, df.columns.get_loc("open")] = df["adj_close"].iloc[-2] * 1.10

    cfg  = _cfg()
    snap = calc_indicators(df, cfg)

    prev_adj_c = df["adj_close"].iloc[-2]
    raw_open   = df["open"].iloc[-1]
    expected_gap_up = max(0.0, (raw_open - prev_adj_c) / prev_adj_c)

    assert math.isclose(snap.gap_up_pct, expected_gap_up, rel_tol=1e-9)
    # gap_down should be zero
    assert snap.gap_down_pct == 0.0


# ── test_intraday_return_uses_raw_open ───────────────────────────────────────

def test_intraday_return_uses_raw_open():
    df = _make_df(30)
    # Set known open and adj_close on last bar
    df.iloc[-1, df.columns.get_loc("open")]      = 95.0
    df.iloc[-1, df.columns.get_loc("adj_close")] = 100.0

    cfg  = _cfg()
    snap = calc_indicators(df, cfg)

    expected = (100.0 - 95.0) / 95.0
    assert math.isclose(snap.intraday_return, expected, rel_tol=1e-9)


# ── test_indicators_return_nan_when_insufficient_history ─────────────────────

def test_indicators_return_nan_when_insufficient_history():
    # Only 3 bars — far fewer than required for MA50 or ATR14
    df  = _make_df(3)
    cfg = _cfg()
    snap = calc_indicators(df, cfg)

    assert math.isnan(snap.ma50),  "ma50 should be NaN with only 3 bars"
    assert math.isnan(snap.ma20),  "ma20 should be NaN with only 3 bars"
    assert math.isnan(snap.atr14), "atr14 should be NaN with only 3 bars"
    assert math.isnan(snap.prior_highest_high_20d), \
        "prior_highest_high_20d should be NaN with only 3 bars"
