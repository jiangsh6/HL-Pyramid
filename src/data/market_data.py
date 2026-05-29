"""
Market data fetching via yfinance (Section 6.3).

fetch_ohlcv() is the only public entry point in v0.1.
All callers should pass required_history_days worth of look-back in their
`start` date so indicators can warm up before the first decision bar.
"""
from __future__ import annotations

import pandas as pd


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
