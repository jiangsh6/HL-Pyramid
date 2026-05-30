"""
Market data fetching.

v0.1 (equity): fetch_ohlcv() via yfinance — returns adj_close.
HL Phase 1:    fetch_ohlcv_hl() via Hyperliquid candle endpoint — returns close/adj_close.
Router:        fetch_ohlcv_dispatch() selects source from config.data.source.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import pandas as pd

if TYPE_CHECKING:
    from src.core.models import BotConfig


def fetch_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    """
    Download OHLCV data via yfinance.

    Returns a DataFrame with columns:
        open, high, low, close, adj_close, volume
    Index: DatetimeIndex (date only, UTC-normalised).

    Parameters
    ----------
    ticker : str
        Equity ticker symbol (e.g. "MU").
    start : str
        Inclusive start date, ISO format "YYYY-MM-DD".
    end : str
        Exclusive end date, ISO format "YYYY-MM-DD".

    Raises
    ------
    ImportError
        If yfinance is not installed.
    ValueError
        If the returned DataFrame is empty or missing required columns.
    """
    try:
        import yfinance as yf
    except ImportError as exc:
        raise ImportError(
            "yfinance is required for market data fetching. "
            "Install it with: pip install yfinance>=0.2.40"
        ) from exc

    raw = yf.download(ticker, start=start, end=end, auto_adjust=False, progress=False)

    if raw.empty:
        raise ValueError(
            f"yfinance returned an empty DataFrame for ticker='{ticker}' "
            f"start='{start}' end='{end}'. Check the ticker and date range."
        )

    # yfinance may return MultiIndex columns when a single ticker is requested;
    # flatten them.
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [col[0] for col in raw.columns]

    # Normalise column names: lower-case, spaces → underscores.
    raw.columns = [c.lower().replace(" ", "_") for c in raw.columns]

    # yfinance calls it "adj_close" after the rename above; ensure it exists.
    col_map = {}
    for col in raw.columns:
        if col in ("adj close", "adj_close"):
            col_map[col] = "adj_close"
    if col_map:
        raw = raw.rename(columns=col_map)

    required = {"open", "high", "low", "close", "adj_close", "volume"}
    missing = required - set(raw.columns)
    if missing:
        raise ValueError(
            f"yfinance response missing columns: {missing}. "
            f"Got: {list(raw.columns)}"
        )

    df = raw[["open", "high", "low", "close", "adj_close", "volume"]].copy()

    # Normalise index to date-only (no timezone).
    if hasattr(df.index, "normalize"):
        df.index = df.index.normalize()
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    return df


def fetch_ohlcv_dispatch(
    source: str,
    ticker_or_coin: str,
    start: str,
    end: str,
    config: Optional["BotConfig"] = None,
) -> pd.DataFrame:
    """
    Route to the correct data source based on `source`.

    Parameters
    ----------
    source : str
        "yfinance" or "hyperliquid".
    ticker_or_coin : str
        Equity ticker (e.g. "MU") for yfinance, or coin name (e.g. "BTC")
        for Hyperliquid.
    start : str
        Start date/datetime, ISO format "YYYY-MM-DD".
    end : str
        End date/datetime, ISO format "YYYY-MM-DD".
    config : BotConfig, optional
        Required for Hyperliquid (reads hl.network, hl.bar_interval).

    Returns
    -------
    pd.DataFrame
        Columns: open, high, low, close, adj_close, volume.
    """
    if source == "yfinance":
        return fetch_ohlcv(ticker_or_coin, start, end)

    if source == "hyperliquid":
        from src.hl.client import HyperliquidClient
        from src.hl.candles import fetch_ohlcv_hl

        if config is None:
            raise ValueError(
                "config is required for Hyperliquid data source"
            )

        hl_cfg   = config.hl or {}
        network  = hl_cfg.get("network", "testnet")
        interval = hl_cfg.get("bar_interval", "4h")

        # Convert ISO date strings to UTC millisecond timestamps
        start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
        end_ms   = int(pd.Timestamp(end,   tz="UTC").timestamp() * 1000)

        client = HyperliquidClient(network=network)
        return fetch_ohlcv_hl(ticker_or_coin, interval, start_ms, end_ms, client)

    raise ValueError(
        f"Unknown data source: '{source}'. Valid values: 'yfinance', 'hyperliquid'."
    )
