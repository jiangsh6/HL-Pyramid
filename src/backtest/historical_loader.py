from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from src.hl.candles import fetch_ohlcv_hl
from src.hl.client import HyperliquidClient


REQUIRED_OHLCV_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]


def _to_ms(value: str) -> int:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def load_historical_candles(
    *,
    coin: str,
    interval: str,
    start: str,
    end: str,
    network: str = "testnet",
    client: Optional[HyperliquidClient] = None,
) -> pd.DataFrame:
    """Load Hyperliquid historical candles for offline research."""
    active_client = client or HyperliquidClient(network=network)
    df = fetch_ohlcv_hl(coin, interval, _to_ms(start), _to_ms(end), active_client)
    return normalize_ohlcv(df)


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    missing = {"open", "high", "low", "close", "volume"} - set(df.columns)
    if missing:
        raise ValueError(f"historical candles missing required columns: {sorted(missing)}")
    out = df.copy()
    if "adj_close" not in out.columns:
        out["adj_close"] = out["close"]
    out = out[REQUIRED_OHLCV_COLUMNS]
    if not isinstance(out.index, pd.DatetimeIndex):
        raise ValueError("historical candles must use a DatetimeIndex")
    return out.sort_index()
