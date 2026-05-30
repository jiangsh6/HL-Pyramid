"""
Fetch OHLCV candles from Hyperliquid's candleSnapshot info endpoint.

fetch_ohlcv_hl(coin, interval, start_ms, end_ms, client) -> pd.DataFrame

The returned DataFrame mirrors the schema produced by yfinance's fetch_ohlcv(),
including an `adj_close` column (= `close` for perpetuals — no split/dividend
adjustment).  This keeps the rest of the pipeline (indicators.py, backtester.py)
fully backward-compatible.

Hyperliquid candle object schema:
    t   int (ms) — bar open time
    T   int (ms) — bar close time
    s   str      — symbol
    i   str      — interval
    o   str      — open price
    h   str      — high price
    l   str      — low price
    c   str      — close price
    v   str      — volume (base asset)
    n   int      — number of trades
"""
from __future__ import annotations

from typing import List

import pandas as pd

from src.hl.client import HyperliquidClient

# Assumed maximum candles per API response; paginate if response equals this.
_MAX_PER_REQUEST = 5_000


def fetch_ohlcv_hl(
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    client: HyperliquidClient,
) -> pd.DataFrame:
    """
    Fetch historical OHLCV candles from Hyperliquid.

    Parameters
    ----------
    coin : str
        Asset name, e.g. "BTC", "ETH", "SOL".
    interval : str
        Candle interval string, e.g. "4h", "1h", "15m".
    start_ms : int
        Start time, UTC milliseconds (inclusive).
    end_ms : int
        End time, UTC milliseconds (inclusive).
    client : HyperliquidClient
        Configured REST client instance.

    Returns
    -------
    pd.DataFrame
        Columns: open, high, low, close, adj_close, volume
        Index:   DatetimeIndex (UTC), bar open time.

    Raises
    ------
    ValueError
        If the API returns an empty response for the requested range.
    """
    all_candles: List[dict] = []
    current_start = start_ms

    while True:
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin":      coin,
                "interval":  interval,
                "startTime": current_start,
                "endTime":   end_ms,
            },
        }
        raw = client.post_info(payload)

        if not raw:
            break

        all_candles.extend(raw)

        # If the response reached the assumed page cap, paginate forward.
        if len(raw) < _MAX_PER_REQUEST:
            break

        # Advance start to just after the close time of the last candle returned.
        last_close_ms = int(raw[-1]["T"])
        current_start = last_close_ms + 1
        if current_start >= end_ms:
            break

    if not all_candles:
        raise ValueError(
            f"Hyperliquid returned no candles for "
            f"coin='{coin}' interval='{interval}' "
            f"start_ms={start_ms} end_ms={end_ms}."
        )

    return _to_dataframe(all_candles)


def _to_dataframe(candles: List[dict]) -> pd.DataFrame:
    """Convert raw Hyperliquid candle dicts into a normalised OHLCV DataFrame."""
    rows = []
    for c in candles:
        rows.append(
            {
                "open":   float(c["o"]),
                "high":   float(c["h"]),
                "low":    float(c["l"]),
                "close":  float(c["c"]),
                "volume": float(c["v"]),
                "_t_ms":  int(c["t"]),   # bar open timestamp (ms)
            }
        )

    df = pd.DataFrame(rows)

    # Index = UTC DatetimeIndex from bar open timestamp
    df.index = pd.to_datetime(df["_t_ms"], unit="ms", utc=True)
    df.index.name = "date"
    df = df.drop(columns=["_t_ms"])

    # adj_close = close for crypto perpetuals (no split/dividend adjustment).
    # This column is required for backward compat with indicators.py and
    # backtester.py, which look for df["adj_close"].
    df["adj_close"] = df["close"]

    required = {"open", "high", "low", "close", "adj_close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Missing columns after candle normalisation: {missing}"
        )

    return df[["open", "high", "low", "close", "adj_close", "volume"]]
