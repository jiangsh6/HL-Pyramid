"""
HL Phase 5 — IndicatorSnapshot bar_time field tests.

All 3 named tests:
  test_bar_time_set_for_tz_aware_index
  test_bar_time_none_for_date_index
  test_bar_time_sets_date_field
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import os
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.core.config_loader import load_config
from src.data.indicators import calc_indicators


def _minimal_df(n: int = 60, index: pd.Index | None = None) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame with enough history for all indicators."""
    rng = np.random.default_rng(42)
    prices = 50_000.0 + np.cumsum(rng.normal(0, 200, n))
    if index is None:
        index = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame(
        {
            "open":      prices - 50,
            "high":      prices + 100,
            "low":       prices - 100,
            "close":     prices,
            "adj_close": prices,
            "volume":    rng.uniform(100, 500, n),
        },
        index=index,
    )
    return df


def _config():
    with patch.dict(os.environ, {"HL_TESTNET_ACCOUNT_ADDRESS": "0x1111111111111111111111111111111111111111"}, clear=False):
        return load_config("config/btc_long_thesis.yaml")


# ── Named tests ───────────────────────────────────────────────────────────────

def test_bar_time_set_for_tz_aware_index():
    """calc_indicators sets bar_time when the index is a tz-aware DatetimeIndex."""
    df  = _minimal_df()
    cfg = _config()
    snap = calc_indicators(df, cfg)

    assert snap.bar_time is not None
    assert isinstance(snap.bar_time, datetime)
    # Must be timezone-aware
    assert snap.bar_time.tzinfo is not None


def test_bar_time_none_for_date_index():
    """calc_indicators leaves bar_time=None when the index is tz-naive or date-only."""
    n = 60
    rng = np.random.default_rng(0)
    prices = 100.0 + np.cumsum(rng.normal(0, 1, n))
    # tz-naive DatetimeIndex (yfinance-style equity data)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    df = pd.DataFrame(
        {
            "open":      prices - 0.5,
            "high":      prices + 1.0,
            "low":       prices - 1.0,
            "close":     prices,
            "adj_close": prices,
            "volume":    rng.uniform(1_000_000, 2_000_000, n),
        },
        index=idx,
    )
    cfg = _config()
    snap = calc_indicators(df, cfg)

    assert snap.bar_time is None


def test_bar_time_sets_date_field():
    """When bar_time is set, snap.date is derived from bar_time.date()."""
    df   = _minimal_df()
    cfg  = _config()
    snap = calc_indicators(df, cfg)

    assert snap.bar_time is not None
    assert snap.date == snap.bar_time.date()


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_bar_time_value_matches_last_index_entry():
    """bar_time equals the last bar's timestamp in the DataFrame."""
    df   = _minimal_df()
    cfg  = _config()
    snap = calc_indicators(df, cfg)

    expected = df.index[-1].to_pydatetime()
    assert snap.bar_time == expected
