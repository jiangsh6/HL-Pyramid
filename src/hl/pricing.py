from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from src.hl.precision import format_hl_price, validate_hl_order_wire

OrderSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class OracleSafePrice:
    mark_price: float
    oracle_price: float
    raw_exit_px: float
    oracle_safe_exit_px: float
    formatted_exit_px: float
    max_deviation_bps: float
    aggressiveness_bps: float


def compute_oracle_safe_exit_price(
    side: OrderSide,
    mark_price: float,
    oracle_price: float | None,
    sz_decimals: int,
    max_oracle_deviation_bps: float,
    aggressiveness_bps: float,
    *,
    is_perp: bool = True,
) -> OracleSafePrice:
    """
    Compute a reduce-only exit limit price that is both modestly aggressive and
    capped near the oracle/mark reference.

    If Hyperliquid oracle price is unavailable, callers pass mark as proxy and
    should keep the configured deviation small. For a long cleanup exit, SELL
    prices are capped no lower than oracle * (1 - max_deviation). BUY exits for
    future short support are capped no higher than oracle * (1 + max_deviation).
    """
    if side not in {"buy", "sell"}:
        raise ValueError(f"unsupported exit side: {side}")
    if mark_price <= 0:
        raise ValueError(f"mark_price must be > 0 (got {mark_price})")

    oracle = float(oracle_price if oracle_price is not None and oracle_price > 0 else mark_price)
    max_bps = max(float(max_oracle_deviation_bps), 0.0)
    aggressive_bps = max(float(aggressiveness_bps), 0.0)
    applied_bps = min(aggressive_bps, max_bps)

    if side == "sell":
        raw = mark_price * (1.0 - aggressive_bps / 10_000.0)
        floor = oracle * (1.0 - max_bps / 10_000.0)
        safe = max(raw, floor)
    else:
        raw = mark_price * (1.0 + aggressive_bps / 10_000.0)
        ceiling = oracle * (1.0 + max_bps / 10_000.0)
        safe = min(raw, ceiling)

    # If aggressiveness was larger than max deviation and mark == oracle, use
    # the clipped candidate explicitly; if mark differs from oracle, the max/min
    # above still enforces the oracle bound.
    if oracle == mark_price:
        if side == "sell":
            safe = max(safe, oracle * (1.0 - applied_bps / 10_000.0))
        else:
            safe = min(safe, oracle * (1.0 + applied_bps / 10_000.0))

    formatted_dec = format_hl_price(
        Decimal(str(safe)),
        sz_decimals,
        is_perp=is_perp,
        side=side,
    )
    # Use qty=0 for price-only validation. The function validates price rules
    # and HL float wire compatibility even when qty is zero.
    validate_hl_order_wire(formatted_dec, Decimal("0"), sz_decimals, is_perp=is_perp)
    return OracleSafePrice(
        mark_price=mark_price,
        oracle_price=oracle,
        raw_exit_px=raw,
        oracle_safe_exit_px=safe,
        formatted_exit_px=float(formatted_dec),
        max_deviation_bps=max_bps,
        aggressiveness_bps=aggressive_bps,
    )


def compute_oracle_safe_entry_price(
    side: OrderSide,
    mark_price: float,
    oracle_price: float | None,
    sz_decimals: int,
    max_oracle_deviation_bps: float,
    aggressiveness_bps: float,
    *,
    is_perp: bool = True,
) -> OracleSafePrice:
    """
    Compute a probe-only entry limit price that is modestly aggressive while
    staying inside an oracle/mark deviation cap.

    For BUY entries, the price is nudged above mark and capped no higher than
    oracle * (1 + max_deviation). SELL support is symmetrical for future use.
    """
    if side not in {"buy", "sell"}:
        raise ValueError(f"unsupported entry side: {side}")
    if mark_price <= 0:
        raise ValueError(f"mark_price must be > 0 (got {mark_price})")

    oracle = float(oracle_price if oracle_price is not None and oracle_price > 0 else mark_price)
    max_bps = max(float(max_oracle_deviation_bps), 0.0)
    aggressive_bps = max(float(aggressiveness_bps), 0.0)
    applied_bps = min(aggressive_bps, max_bps)

    if side == "buy":
        raw = mark_price * (1.0 + aggressive_bps / 10_000.0)
        ceiling = oracle * (1.0 + max_bps / 10_000.0)
        safe = min(raw, ceiling)
    else:
        raw = mark_price * (1.0 - aggressive_bps / 10_000.0)
        floor = oracle * (1.0 - max_bps / 10_000.0)
        safe = max(raw, floor)

    if oracle == mark_price:
        if side == "buy":
            safe = min(safe, oracle * (1.0 + applied_bps / 10_000.0))
        else:
            safe = max(safe, oracle * (1.0 - applied_bps / 10_000.0))

    formatted_dec = format_hl_price(
        Decimal(str(safe)),
        sz_decimals,
        is_perp=is_perp,
        side=side,
    )
    validate_hl_order_wire(formatted_dec, Decimal("0"), sz_decimals, is_perp=is_perp)
    return OracleSafePrice(
        mark_price=mark_price,
        oracle_price=oracle,
        raw_exit_px=raw,
        oracle_safe_exit_px=safe,
        formatted_exit_px=float(formatted_dec),
        max_deviation_bps=max_bps,
        aggressiveness_bps=aggressive_bps,
    )
