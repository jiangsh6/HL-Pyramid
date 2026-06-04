from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.backtest.historical_loader import load_historical_candles  # noqa: E402
from src.hl.client import HyperliquidClient  # noqa: E402
from src.hl.funding import FundingPayment, get_funding_history  # noqa: E402


DEFAULT_INTERVALS = ["1d", "4h", "1h"]
FUNDING_PAGE_LIMIT_GUARD = 1000
MAX_CANDLES_PER_CHUNK = 4_000


def _to_ms(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _from_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _interval_ms(interval: str) -> int:
    if interval.endswith("h"):
        return int(interval[:-1]) * 60 * 60 * 1000
    if interval.endswith("d"):
        return int(interval[:-1]) * 24 * 60 * 60 * 1000
    raise ValueError(f"unsupported interval for chunked HYPE fetch: {interval}")


def fetch_candles_chunked(
    *,
    symbol: str,
    interval: str,
    start: str,
    end: str,
    network: str,
    client: HyperliquidClient,
) -> "pd.DataFrame":
    import pandas as pd

    start_ms = _to_ms(start)
    end_ms = _to_ms(end)
    step_ms = _interval_ms(interval) * MAX_CANDLES_PER_CHUNK
    frames = []
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(cursor + step_ms, end_ms)
        try:
            frame = load_historical_candles(
                coin=symbol,
                interval=interval,
                start=_from_ms(cursor),
                end=_from_ms(chunk_end),
                network=network,
                client=client,
            )
        except ValueError as exc:
            if "returned no candles" not in str(exc):
                raise
            frame = None
        if frame is not None and not frame.empty:
            frames.append(frame)
        cursor = chunk_end + _interval_ms(interval)
    if not frames:
        raise ValueError(f"Hyperliquid returned no {symbol} {interval} candles for requested range")
    return pd.concat(frames).sort_index().loc[lambda df: ~df.index.duplicated(keep="last")]


def fetch_funding_history_paginated(
    symbol: str,
    start_ms: int,
    end_ms: int,
    client: HyperliquidClient,
) -> list[FundingPayment]:
    """Fetch all funding rows across the requested window.

    Hyperliquid fundingHistory responses are capped, so a single request can
    silently return only the first page. Paginating by the last returned
    timestamp keeps the research cost model aligned with the full backtest
    period without changing live execution code.
    """
    cursor = start_ms
    rows: list[FundingPayment] = []
    seen: set[tuple[str, int]] = set()
    for _ in range(FUNDING_PAGE_LIMIT_GUARD):
        page = get_funding_history(symbol, cursor, end_ms, client)
        if not page:
            break
        new_rows = 0
        max_seen_ms = cursor
        for payment in page:
            payment_ms = int(payment.timestamp.timestamp() * 1000)
            max_seen_ms = max(max_seen_ms, payment_ms)
            key = (payment.coin, payment_ms)
            if key in seen:
                continue
            seen.add(key)
            rows.append(payment)
            new_rows += 1
        next_cursor = max_seen_ms + 1
        if new_rows == 0 or next_cursor <= cursor or next_cursor >= end_ms:
            break
        cursor = next_cursor
    return sorted(rows, key=lambda item: item.timestamp)


def fetch_hype_data(
    *,
    symbol: str = "HYPE",
    network: str = "mainnet",
    start: str,
    end: str,
    data_dir: Path,
    intervals: list[str] | None = None,
    refresh: bool = False,
) -> dict[str, Path]:
    """Fetch read-only Hyperliquid candles and funding records to local CSV."""
    intervals = intervals or DEFAULT_INTERVALS
    data_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    client = HyperliquidClient(network=network)
    for interval in intervals:
        path = data_dir / f"{symbol}_{interval}.csv"
        if path.exists() and not refresh:
            print(f"cache_hit interval={interval} path={path}")
            written[interval] = path
            continue
        df = fetch_candles_chunked(symbol=symbol, interval=interval, start=start, end=end, network=network, client=client)
        df.to_csv(path, index_label="timestamp")
        print(f"wrote interval={interval} rows={len(df)} start={df.index.min()} end={df.index.max()} path={path}")
        written[interval] = path

    funding_path = data_dir / f"{symbol}_funding.csv"
    if funding_path.exists() and not refresh:
        print(f"cache_hit funding path={funding_path}")
        written["funding"] = funding_path
        return written
    payments = fetch_funding_history_paginated(symbol, _to_ms(start), _to_ms(end), client)
    with funding_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "funding_rate", "position_usd", "payment_usd"])
        writer.writeheader()
        for payment in payments:
            writer.writerow(
                {
                    "timestamp": payment.timestamp.isoformat(),
                    "funding_rate": payment.funding_rate,
                    "position_usd": payment.position_usd,
                    "payment_usd": payment.payment_usd,
                }
            )
    print(f"wrote funding rows={len(payments)} path={funding_path}")
    written["funding"] = funding_path
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch HYPE Hyperliquid candles and funding for research backtests")
    parser.add_argument("--symbol", default="HYPE")
    parser.add_argument("--network", default="mainnet", choices=["mainnet", "testnet"])
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--data-dir", default="data/hyperliquid")
    parser.add_argument("--intervals", default="1d,4h,1h")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    intervals = [item.strip() for item in args.intervals.split(",") if item.strip()]
    fetch_hype_data(
        symbol=args.symbol,
        network=args.network,
        start=args.start,
        end=args.end,
        data_dir=Path(args.data_dir),
        intervals=intervals,
        refresh=args.refresh,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
