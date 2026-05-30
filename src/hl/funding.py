"""Hyperliquid funding-rate helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional

from src.core.models import ThesisState
from src.hl.client import HyperliquidClient


@dataclass(frozen=True)
class FundingPayment:
    coin: str
    timestamp: datetime
    funding_rate: float
    position_usd: float
    payment_usd: float


def _to_utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if value > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return _to_utc_datetime(float(value))
        except ValueError:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc)
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_funding_history(
    coin: str,
    start_ms: int,
    end_ms: int,
    client: HyperliquidClient,
) -> List[FundingPayment]:
    """Fetch and parse Hyperliquid hourly funding history."""
    try:
        response = client.post_info({
            "type": "fundingHistory",
            "coin": coin,
            "startTime": start_ms,
            "endTime": end_ms,
        })
    except Exception:
        return []

    if not response:
        return []
    if not isinstance(response, list):
        return []

    payments: List[FundingPayment] = []
    for item in response:
        if not isinstance(item, dict):
            continue
        rate = _as_float(item.get("fundingRate", item.get("funding_rate")))
        position_usd = _as_float(
            item.get("positionUsd", item.get("position_usd", item.get("notionalUsd")))
        )
        payments.append(FundingPayment(
            coin=str(item.get("coin", coin)),
            timestamp=_to_utc_datetime(item.get("time", item.get("timestamp"))),
            funding_rate=rate,
            position_usd=position_usd,
            payment_usd=-1.0 * rate * position_usd,
        ))
    return payments


def get_predicted_funding(
    coin: str,
    client: HyperliquidClient,
) -> Optional[float]:
    """Return the current predicted hourly funding rate for `coin`."""
    try:
        response = client.post_info({"type": "metaAndAssetCtxs"})
    except Exception:
        return None

    if not isinstance(response, list) or len(response) < 2:
        return None
    meta, asset_ctxs = response[0], response[1]
    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    if not isinstance(universe, list) or not isinstance(asset_ctxs, list):
        return None

    for idx, asset in enumerate(universe):
        if not isinstance(asset, dict) or asset.get("name") != coin:
            continue
        if idx >= len(asset_ctxs) or not isinstance(asset_ctxs[idx], dict):
            return None
        try:
            return float(asset_ctxs[idx]["funding"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def calc_funding_payment(
    funding_rate: float,
    position_contracts: float,
    mark_price: float,
    hours: float = 1.0,
) -> float:
    """Calculate funding payment for a long perp position."""
    return -1.0 * funding_rate * position_contracts * mark_price * hours


def apply_funding_to_state(
    state: ThesisState,
    payment_usd: float,
) -> ThesisState:
    """Apply funding PnL to a copied ThesisState and return the updated copy."""
    updated = state.model_copy(deep=True)
    updated.cumulative_funding_pnl += payment_usd
    updated.thesis_pnl += payment_usd
    updated.unrealized_pnl += payment_usd
    return updated
