from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


DEFAULT_RUN_DIR = Path("logs/paper_trade")
DEFAULT_DATA_DIR = Path("data/hyperliquid")
TARGET_EPISODES = 15


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _parse_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, format="mixed")


def _first_available_timestamp(*frames: pd.DataFrame) -> pd.Timestamp | None:
    candidates = []
    for frame in frames:
        if not frame.empty and "timestamp" in frame.columns:
            candidates.append(_parse_ts(frame["timestamp"]).min())
        elif not frame.empty and "entry_time" in frame.columns:
            candidates.append(_parse_ts(frame["entry_time"]).min())
    return min(candidates) if candidates else None


def _count_bull_episodes(data_dir: Path, start: pd.Timestamp | None) -> int:
    path = data_dir / "HYPE_1d.csv"
    if not path.exists():
        return 0
    df = pd.read_csv(path)
    if df.empty or "close" not in df.columns:
        return 0
    ts_col = "timestamp" if "timestamp" in df.columns else df.columns[0]
    df[ts_col] = _parse_ts(df[ts_col])
    if start is not None:
        df = df[df[ts_col] >= start]
    ma100 = df["close"].rolling(100, min_periods=100).mean()
    bull = (df["close"] > ma100).fillna(False)
    count = 0
    prev = False
    for flag in bull.astype(bool):
        if flag and not prev:
            count += 1
        prev = bool(flag)
    return count


def _sum_column(df: pd.DataFrame, names: list[str]) -> float:
    for name in names:
        if name in df.columns:
            return float(pd.to_numeric(df[name], errors="coerce").fillna(0.0).sum())
    return 0.0


def build_progress_report(run_dir: Path = DEFAULT_RUN_DIR, data_dir: Path = DEFAULT_DATA_DIR) -> str:
    fills = _load_csv(run_dir / "fills.csv")
    orders = _load_csv(run_dir / "orders.csv")
    positions = _load_csv(run_dir / "positions.csv")
    start = _first_available_timestamp(fills, orders, positions)
    now = pd.Timestamp(datetime.now(timezone.utc))
    days_running = int((now - start).days) if start is not None else 0
    current_state = "FLAT"
    if not positions.empty:
        qty_col = next((c for c in ("position_qty", "qty", "current_position_qty") if c in positions.columns), None)
        if qty_col and float(pd.to_numeric(positions[qty_col], errors="coerce").fillna(0.0).iloc[-1]) != 0.0:
            current_state = "IN_TRADE"
    closed_trades = int((fills.get("side", pd.Series(dtype=str)).astype(str).str.lower() == "sell").sum()) if not fills.empty else 0
    open_trades = 1 if current_state == "IN_TRADE" else 0
    net_pnl = _sum_column(fills, ["net_pnl", "pnl", "realized_pnl"])
    funding_paid = _sum_column(fills, ["funding_paid", "funding_pnl", "funding_payment"])
    episodes = _count_bull_episodes(data_dir, start)
    start_text = start.date().isoformat() if start is not None else "-"

    return f"""Paper Trade Progress Report
============================
Start date: {start_text}
Days running: {days_running}
Current state: {current_state}
Independent bull episodes observed: {episodes} / {TARGET_EPISODES} target

Closed trades: {closed_trades}
Open trades: {open_trades}
Cumulative net PnL: {net_pnl:.2f} USDC
Cumulative funding paid: {funding_paid:.2f} USDC

Backtest baseline (for comparison):
  Expected trades per 90 days: ~2 (based on 9 trades / 452 days)
  Expected net return on $1500: ~$30-50 over 6 months

Ready-for-mainnet checks:
  [ ] Sample size >= 15 episodes (current: {episodes})
  [ ] Slippage within +/-20% of backtest assumption (current: unknown)
  [ ] At least one complete STARTER -> STOP_HIT cycle observed
  [ ] Backtest rerun with new data still shows starter-only leading
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print HYPE paper-trade progress from local CSV logs")
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    args = parser.parse_args(argv)
    print(build_progress_report(Path(args.run_dir), Path(args.data_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
