from __future__ import annotations

import numpy as np
import pandas as pd

from src.core.models import BotConfig, IndicatorSnapshot


def calc_indicators(df: pd.DataFrame, cfg: BotConfig) -> IndicatorSnapshot:
    """
    Compute all indicators for the latest bar in df.

    df must have columns: open, high, low, close, adj_close, volume
    with a DatetimeIndex.  The slice passed in should contain only bars
    up to and including bar T (no look-ahead).

    Returns IndicatorSnapshot for the last row, or a snapshot with NaN
    values when history is insufficient (caller must check for NaN and halt).
    """
    ind_cfg     = cfg.data.get("indicators", {})
    ma_windows  = ind_cfg.get("ma_windows", [5, 10, 20, 50])
    atr_window  = ind_cfg.get("atr_window", 14)
    vol_window  = ind_cfg.get("volume_window", 20)
    high_window = ind_cfg.get("high_breakout_window", 20)

    # Use adj_close when present (yfinance / equity path); fall back to close
    # when only the raw crypto column is available (Hyperliquid path).
    adj   = df["adj_close"] if "adj_close" in df.columns else df["close"]
    raw_h = df["high"]
    raw_l = df["low"]
    raw_o = df["open"]
    vol   = df["volume"]

    # Moving averages on adj_close
    ma: dict[int, pd.Series] = {}
    for w in ma_windows:
        ma[w] = adj.rolling(w).mean()

    # ATR on raw OHLC
    prev_close = adj.shift(1)
    tr = pd.concat(
        [
            raw_h - raw_l,
            (raw_h - prev_close).abs(),
            (raw_l - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_window).mean()

    # Volume average
    avg_vol = vol.rolling(vol_window).mean()

    # prior_highest_high_20d: shifted by 1 bar to avoid look-ahead
    prior_high_20d = raw_h.rolling(high_window).max().shift(1)

    # Derived fields — all on the final row
    last_adj   = float(adj.iloc[-1])
    last_open  = float(raw_o.iloc[-1])
    last_high  = float(raw_h.iloc[-1])
    last_low   = float(raw_l.iloc[-1])
    last_vol   = float(vol.iloc[-1])
    prev_adj_c = float(adj.iloc[-2]) if len(adj) >= 2 else float("nan")

    ma5_val  = float(ma[5].iloc[-1])  if 5  in ma else float("nan")
    ma10_val = float(ma[10].iloc[-1]) if 10 in ma else float("nan")
    ma20_val = float(ma[20].iloc[-1]) if 20 in ma else float("nan")
    ma50_val = float(ma[50].iloc[-1]) if 50 in ma else float("nan")

    atr14_val   = float(atr.iloc[-1])
    avg_vol_val = float(avg_vol.iloc[-1])
    prior_hh    = float(prior_high_20d.iloc[-1])

    # drawdown_from_20d_high
    if not np.isnan(prior_hh) and prior_hh > 0:
        dd_20d = (prior_hh - last_adj) / prior_hh
    else:
        dd_20d = float("nan")

    # distance_from_ma10/20
    dist_ma10 = (last_adj - ma10_val) / ma10_val if not np.isnan(ma10_val) and ma10_val != 0 else float("nan")
    dist_ma20 = (last_adj - ma20_val) / ma20_val if not np.isnan(ma20_val) and ma20_val != 0 else float("nan")

    # intraday_return: (adj_close - raw_open) / raw_open
    intraday_ret = (last_adj - last_open) / last_open if last_open != 0 else float("nan")

    # gap_up_pct / gap_down_pct — use raw open vs prev adj_close
    if not np.isnan(prev_adj_c) and prev_adj_c != 0:
        gap_up   = max(0.0, (last_open - prev_adj_c) / prev_adj_c)
        gap_down = max(0.0, (prev_adj_c - last_open) / prev_adj_c)
    else:
        gap_up   = float("nan")
        gap_down = float("nan")

    bar_date = df.index[-1]
    if hasattr(bar_date, "date"):
        bar_date = bar_date.date()

    return IndicatorSnapshot(
        date=bar_date,
        adj_close=last_adj,
        open=last_open,
        high=last_high,
        low=last_low,
        volume=last_vol,
        prev_adj_close=prev_adj_c,
        ma5=ma5_val,
        ma10=ma10_val,
        ma20=ma20_val,
        ma50=ma50_val,
        atr14=atr14_val,
        avg_volume_20d=avg_vol_val,
        prior_highest_high_20d=prior_hh,
        drawdown_from_20d_high=dd_20d,
        distance_from_ma10=dist_ma10,
        distance_from_ma20=dist_ma20,
        intraday_return=intraday_ret,
        gap_up_pct=gap_up,
        gap_down_pct=gap_down,
    )
