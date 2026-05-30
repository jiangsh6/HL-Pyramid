"""
HL Phase 1 — fetch_ohlcv_hl tests.

All 4 named tests:
  test_fetch_ohlcv_hl_returns_correct_columns
  test_fetch_ohlcv_hl_index_is_utc_datetime
  test_fetch_ohlcv_hl_handles_empty_response
  test_fetch_ohlcv_hl_interval_4h_correct
"""
from __future__ import annotations

import math
from datetime import timezone
from unittest.mock import patch

import pandas as pd
import pytest

from src.hl.candles import fetch_ohlcv_hl
from src.hl.client import HyperliquidClient


# ── Shared helpers ───────────────────────────────────────────────────────────

def _make_candle(t_ms: int, o="50000.0", h="51000.0", l="49000.0",
                 c="50500.0", v="100.5") -> dict:
    """Build a minimal Hyperliquid candle dict."""
    interval_ms = 4 * 60 * 60 * 1000   # 4h in ms
    return {
        "t": t_ms,
        "T": t_ms + interval_ms - 1,
        "s": "BTC",
        "i": "4h",
        "o": o, "h": h, "l": l, "c": c, "v": v,
        "n": 12345,
    }


_EPOCH_MS  = 1_672_531_200_000   # 2023-01-01 00:00:00 UTC
_4H_MS     = 4 * 60 * 60 * 1000


def _mock_single_page(n: int = 3):
    """Return a list of n sequential 4h candles starting at _EPOCH_MS."""
    return [_make_candle(_EPOCH_MS + i * _4H_MS) for i in range(n)]


# ── Named tests ──────────────────────────────────────────────────────────────

def test_fetch_ohlcv_hl_returns_correct_columns():
    """DataFrame must have exactly: open, high, low, close, adj_close, volume."""
    client = HyperliquidClient(network="testnet")

    with patch.object(client, "post_info", return_value=_mock_single_page(5)):
        df = fetch_ohlcv_hl("BTC", "4h", _EPOCH_MS, _EPOCH_MS + 10 * _4H_MS, client)

    assert set(df.columns) == {"open", "high", "low", "close", "adj_close", "volume"}
    assert len(df) == 5


def test_fetch_ohlcv_hl_index_is_utc_datetime():
    """Index must be a timezone-aware DatetimeIndex (UTC)."""
    client = HyperliquidClient(network="testnet")

    with patch.object(client, "post_info", return_value=_mock_single_page(3)):
        df = fetch_ohlcv_hl("BTC", "4h", _EPOCH_MS, _EPOCH_MS + 5 * _4H_MS, client)

    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None
    assert str(df.index.tz) == "UTC"

    # First bar should start at _EPOCH_MS
    expected_first = pd.Timestamp(_EPOCH_MS, unit="ms", tz="UTC")
    assert df.index[0] == expected_first


def test_fetch_ohlcv_hl_handles_empty_response():
    """An empty API response must raise ValueError, not silently return empty DF."""
    client = HyperliquidClient(network="testnet")

    with patch.object(client, "post_info", return_value=[]):
        with pytest.raises(ValueError, match="no candles"):
            fetch_ohlcv_hl("BTC", "4h", _EPOCH_MS, _EPOCH_MS + _4H_MS, client)


def test_fetch_ohlcv_hl_interval_4h_correct():
    """post_info must be called with interval='4h' and the correct payload shape."""
    client = HyperliquidClient(network="testnet")
    captured_payloads = []

    def capturing_post_info(payload):
        captured_payloads.append(payload)
        return _mock_single_page(2)

    with patch.object(client, "post_info", side_effect=capturing_post_info):
        fetch_ohlcv_hl("BTC", "4h", _EPOCH_MS, _EPOCH_MS + 5 * _4H_MS, client)

    assert len(captured_payloads) >= 1
    req = captured_payloads[0]
    assert req["type"] == "candleSnapshot"
    assert req["req"]["coin"] == "BTC"
    assert req["req"]["interval"] == "4h"
    assert req["req"]["startTime"] == _EPOCH_MS


# ── Extra coverage ───────────────────────────────────────────────────────────

def test_adj_close_equals_close():
    """For crypto perps, adj_close must equal close (no split adjustment)."""
    client = HyperliquidClient(network="testnet")

    with patch.object(client, "post_info", return_value=_mock_single_page(3)):
        df = fetch_ohlcv_hl("BTC", "4h", _EPOCH_MS, _EPOCH_MS + 5 * _4H_MS, client)

    assert (df["adj_close"] == df["close"]).all()


def test_float_conversion():
    """String prices from the API must be converted to float."""
    client = HyperliquidClient(network="testnet")

    with patch.object(client, "post_info", return_value=_mock_single_page(1)):
        df = fetch_ohlcv_hl("BTC", "4h", _EPOCH_MS, _EPOCH_MS + _4H_MS, client)

    for col in ("open", "high", "low", "close", "adj_close", "volume"):
        assert df[col].dtype in (float, "float64")
    assert math.isclose(df["open"].iloc[0], 50000.0)
    assert math.isclose(df["close"].iloc[0], 50500.0)
