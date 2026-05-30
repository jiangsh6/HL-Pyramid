"""
Hyperliquid account state — Phase 3.

get_account_snapshot(wallet_address, client) -> HLAccountSnapshot

Parses the /info clearinghouseState response into typed dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


class HLAPIError(Exception):
    """Raised when the HL API returns an unexpected or malformed response."""


@dataclass
class HLPosition:
    coin: str
    contracts: float          # positive = long, negative = short
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    liquidation_price: Optional[float]
    margin_used: float
    leverage: float


@dataclass
class HLAccountSnapshot:
    account_value: float
    margin_used: float
    withdrawable: float
    positions: List[HLPosition] = field(default_factory=list)


def get_account_snapshot(wallet_address: str, client: Any) -> HLAccountSnapshot:
    """
    POST /info with type="clearinghouseState" and parse the response.

    Returns HLAccountSnapshot with empty positions list if wallet has no positions.
    Raises HLAPIError on bad/unexpected response.
    """
    payload = {"type": "clearinghouseState", "user": wallet_address}

    try:
        resp = client.post_info(payload)
    except Exception as exc:
        raise HLAPIError(f"Failed to fetch clearinghouseState: {exc}") from exc

    if not isinstance(resp, dict):
        raise HLAPIError(f"Expected dict response, got {type(resp).__name__}")

    margin_summary = resp.get("marginSummary")
    if not isinstance(margin_summary, dict):
        raise HLAPIError("Response missing 'marginSummary' dict")

    try:
        account_value = float(margin_summary.get("accountValue", 0))
        margin_used   = float(margin_summary.get("totalMarginUsed", 0))
        withdrawable  = float(margin_summary.get("withdrawable", 0))
    except (TypeError, ValueError) as exc:
        raise HLAPIError(f"Failed to parse marginSummary fields: {exc}") from exc

    asset_positions = resp.get("assetPositions", [])
    if not isinstance(asset_positions, list):
        raise HLAPIError("'assetPositions' is not a list")

    positions: List[HLPosition] = []
    for entry in asset_positions:
        pos = entry.get("position") if isinstance(entry, dict) else entry
        if not isinstance(pos, dict):
            raise HLAPIError(f"Unexpected position entry format: {entry}")

        try:
            coin           = str(pos["coin"])
            contracts      = float(pos["szi"])
            entry_price    = float(pos.get("entryPx", 0) or 0)
            unrealized_pnl = float(pos.get("unrealizedPnl", 0) or 0)
            margin_used_pos = float(pos.get("marginUsed", 0) or 0)

            liq_px_raw = pos.get("liquidationPx")
            liq_px: Optional[float] = float(liq_px_raw) if liq_px_raw is not None else None

            lev_raw = pos.get("leverage", 1)
            leverage = float(lev_raw.get("value", 1)) if isinstance(lev_raw, dict) else float(lev_raw)

            mark_px_raw = pos.get("markPx")
            if mark_px_raw is not None:
                mark_price = float(mark_px_raw)
            elif abs(contracts) > 0:
                pos_val    = float(pos.get("positionValue", 0) or 0)
                mark_price = abs(pos_val) / abs(contracts)
            else:
                mark_price = entry_price

        except (KeyError, TypeError, ValueError) as exc:
            raise HLAPIError(f"Failed to parse position entry: {exc}") from exc

        positions.append(HLPosition(
            coin=coin,
            contracts=contracts,
            entry_price=entry_price,
            mark_price=mark_price,
            unrealized_pnl=unrealized_pnl,
            liquidation_price=liq_px,
            margin_used=margin_used_pos,
            leverage=leverage,
        ))

    return HLAccountSnapshot(
        account_value=account_value,
        margin_used=margin_used,
        withdrawable=withdrawable,
        positions=positions,
    )
