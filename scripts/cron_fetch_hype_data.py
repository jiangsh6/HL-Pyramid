from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.fetch_hype_hl_data import (  # noqa: E402
    _to_ms,
    fetch_candles_chunked,
    fetch_funding_history_paginated,
)
from src.hl.client import HyperliquidClient  # noqa: E402


DEFAULT_INTERVALS = ["1d", "4h", "1h"]
DEFAULT_DATA_DIR = Path("data/hyperliquid")
DEFAULT_LOG_PATH = Path("logs/data_fetch.log")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _timestamp_column(df: pd.DataFrame) -> str:
    for candidate in ("timestamp", "time", "date"):
        if candidate in df.columns:
            return candidate
    raise ValueError(f"CSV missing timestamp column: {list(df.columns)}")


def _last_timestamp(df: pd.DataFrame) -> pd.Timestamp | None:
    if df.empty:
        return None
    col = _timestamp_column(df)
    return pd.to_datetime(df[col], utc=True, format="mixed").max()


def _merge_dedupe(existing: pd.DataFrame, new: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if existing.empty:
        merged = new.copy()
        before = 0
    elif new.empty:
        merged = existing.copy()
        before = len(existing)
    else:
        merged = pd.concat([existing, new], ignore_index=True)
        before = len(existing)
    if merged.empty:
        return merged, 0
    col = _timestamp_column(merged)
    merged["_ts_sort"] = pd.to_datetime(merged[col], utc=True, format="mixed")
    merged = merged.sort_values("_ts_sort").drop_duplicates(subset=["_ts_sort"], keep="last")
    merged[col] = merged["_ts_sort"].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    merged[col] = merged[col].str.replace(r"(\+0000)$", "+00:00", regex=True)
    merged = merged.drop(columns=["_ts_sort"])
    return merged, max(len(merged) - before, 0)


def _append_candles(
    *,
    symbol: str,
    interval: str,
    data_dir: Path,
    network: str,
    client: HyperliquidClient,
    end: str,
) -> tuple[Path, int, int]:
    path = data_dir / f"{symbol}_{interval}.csv"
    existing = _load_csv(path)
    last = _last_timestamp(existing)
    start = last.isoformat() if last is not None else "2020-01-01T00:00:00+00:00"
    fetched = fetch_candles_chunked(symbol=symbol, interval=interval, start=start, end=end, network=network, client=client)
    new = fetched.reset_index().rename(columns={"index": "timestamp"})
    if "timestamp" not in new.columns:
        new = new.rename(columns={new.columns[0]: "timestamp"})
    merged, added = _merge_dedupe(existing, new)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(path, index=False)
    return path, added, len(merged)


def _append_funding(
    *,
    symbol: str,
    data_dir: Path,
    network: str,
    client: HyperliquidClient,
    end: str,
) -> tuple[Path, int, int]:
    path = data_dir / f"{symbol}_funding.csv"
    existing = _load_csv(path)
    last = _last_timestamp(existing)
    start = last.isoformat() if last is not None else "2020-01-01T00:00:00+00:00"
    rows = fetch_funding_history_paginated(symbol, _to_ms(start), _to_ms(end), client)
    new = pd.DataFrame(
        [
            {
                "timestamp": row.timestamp.isoformat(),
                "funding_rate": row.funding_rate,
                "position_usd": row.position_usd,
                "payment_usd": row.payment_usd,
            }
            for row in rows
        ]
    )
    merged, added = _merge_dedupe(existing, new)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(path, index=False)
    return path, added, len(merged)


def append_hype_data(
    *,
    symbol: str = "HYPE",
    network: str = "mainnet",
    data_dir: Path = DEFAULT_DATA_DIR,
    log_path: Path = DEFAULT_LOG_PATH,
    intervals: list[str] | None = None,
    end: str | None = None,
    client: HyperliquidClient | None = None,
) -> dict[str, dict[str, Any]]:
    intervals = intervals or DEFAULT_INTERVALS
    end = end or _now_iso()
    active_client = client or HyperliquidClient(network=network)
    data_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for interval in intervals:
        try:
            path, added, final_rows = _append_candles(
                symbol=symbol,
                interval=interval,
                data_dir=data_dir,
                network=network,
                client=active_client,
                end=end,
            )
            results[interval] = {"path": str(path), "added_rows": added, "final_rows": final_rows}
        except Exception as exc:  # fail loudly after logging every failed component
            errors.append(f"{interval}: {exc}")

    try:
        path, added, final_rows = _append_funding(symbol=symbol, data_dir=data_dir, network=network, client=active_client, end=end)
        results["funding"] = {"path": str(path), "added_rows": added, "final_rows": final_rows}
    except Exception as exc:
        errors.append(f"funding: {exc}")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            _now_iso(),
            symbol,
            network,
            "; ".join(f"{k}: added={v['added_rows']} final={v['final_rows']}" for k, v in sorted(results.items())),
            "ERROR " + " | ".join(errors) if errors else "OK",
        ])

    if errors:
        raise RuntimeError("; ".join(errors))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Incrementally append HYPE Hyperliquid data for paper-trade research")
    parser.add_argument("--symbol", default="HYPE")
    parser.add_argument("--network", default="mainnet", choices=["mainnet", "testnet"])
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    parser.add_argument("--intervals", default="1d,4h,1h")
    parser.add_argument("--end", default=None)
    args = parser.parse_args(argv)
    intervals = [item.strip() for item in args.intervals.split(",") if item.strip()]
    try:
        results = append_hype_data(
            symbol=args.symbol,
            network=args.network,
            data_dir=Path(args.data_dir),
            log_path=Path(args.log_path),
            intervals=intervals,
            end=args.end,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for key, value in sorted(results.items()):
        print(f"{key}: added={value['added_rows']} final={value['final_rows']} path={value['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
