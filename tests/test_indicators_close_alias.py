"""
HL Phase 1 — Indicator alias tests (close vs adj_close column).

All 4 named tests:
  test_indicators_work_with_adj_close_column
  test_indicators_work_with_close_only_column
  test_prior_highest_high_still_shifted_on_hl_data
  test_indicator_snapshot_close_field_always_populated
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.core.config_loader import load_config
from src.data.indicators import calc_indicators
from tests._helpers import base_config, make_indicators


CONFIG_PATH = "config/mu_long_thesis.yaml"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_equity_df(n: int = 60) -> pd.DataFrame:
    """Standard yfinance-style DataFrame with adj_close column."""
    rng = np.random.default_rng(7)
    closes = 100 + np.cumsum(rng.normal(0.3, 1.0, n))
    opens  = closes - rng.uniform(0, 1.0, n)
    highs  = closes + rng.uniform(0.1, 2.0, n)
    lows   = np.minimum(opens, closes) - rng.uniform(0, 1.0, n)
    vols   = rng.integers(1_000_000, 3_000_000, n).astype(float)
    idx = pd.date_range("2025-01-02", periods=n, freq="B")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "adj_close": closes, "volume": vols,
    }, index=idx)


def _make_hl_df(n: int = 60) -> pd.DataFrame:
    """
    Simulated Hyperliquid-style DataFrame — close column present, no adj_close.
    (The real fetch_ohlcv_hl adds adj_close=close; this tests the fallback path.)
    """
    df = _make_equity_df(n)
    df = df.drop(columns=["adj_close"])     # remove adj_close to force fallback
    return df


# ── Named tests ──────────────────────────────────────────────────────────────

def test_indicators_work_with_adj_close_column():
    """Existing equity path (adj_close present) must continue to work."""
    cfg = base_config()
    df  = _make_equity_df(60)
    snap = calc_indicators(df, cfg)

    assert not math.isnan(snap.ma20)
    assert not math.isnan(snap.ma50)
    assert snap.adj_close > 0
    # close field must equal adj_close (populated by model_validator)
    assert math.isclose(snap.close, snap.adj_close)


def test_indicators_work_with_close_only_column():
    """
    When only 'close' is present (HL fallback), indicators must compute
    without error and return valid (non-NaN) values.
    """
    cfg = base_config()
    df  = _make_hl_df(60)

    assert "adj_close" not in df.columns
    assert "close" in df.columns

    snap = calc_indicators(df, cfg)

    assert not math.isnan(snap.ma20)
    assert not math.isnan(snap.ma50)
    assert snap.adj_close > 0      # adj_close field is populated from the close series
    assert snap.close > 0


def test_prior_highest_high_still_shifted_on_hl_data():
    """
    The look-ahead guard (shift(1)) on prior_highest_high_20d must work
    identically whether the price series comes from adj_close or close.
    """
    cfg = base_config()
    df  = _make_hl_df(60)

    snap = calc_indicators(df, cfg)

    # prior_highest_high_20d should equal rolling_max(high,20).shift(1).iloc[-1]
    expected = df["high"].rolling(20).max().shift(1).iloc[-1]
    assert math.isclose(snap.prior_highest_high_20d, expected, rel_tol=1e-9)


def test_indicator_snapshot_close_field_always_populated():
    """
    IndicatorSnapshot.close must be non-None and equal to adj_close
    regardless of which input column was used.
    """
    cfg = base_config()

    # Equity path
    df_eq = _make_equity_df(60)
    snap_eq = calc_indicators(df_eq, cfg)
    assert snap_eq.close is not None
    assert math.isclose(snap_eq.close, snap_eq.adj_close)

    # HL fallback path
    df_hl = _make_hl_df(60)
    snap_hl = calc_indicators(df_hl, cfg)
    assert snap_hl.close is not None
    assert math.isclose(snap_hl.close, snap_hl.adj_close)


# ── make_indicators backward compat ─────────────────────────────────────────

def test_make_indicators_helper_populates_close():
    """
    The shared _helpers.make_indicators() factory (used by all Phase 1-5 tests)
    does not pass close= explicitly. The model_validator must auto-populate it.
    """
    snap = make_indicators(adj_close=123.45)
    assert snap.close is not None
    assert math.isclose(snap.close, 123.45)


def test_make_indicators_close_override():
    """
    If close is explicitly passed it must not be overwritten by the validator.
    (Useful when testing HL-specific behaviour where close != adj_close.)
    """
    snap = make_indicators(adj_close=100.0, close=99.0)
    assert math.isclose(snap.close, 99.0)   # explicit value preserved
    assert math.isclose(snap.adj_close, 100.0)
