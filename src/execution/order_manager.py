"""
Order lifecycle helpers.

In paper mode, fills are immediate; no real order state machine is needed.
HL Phase 4 adds exchange-side tracking fields (cloid, hl_oid, exchange_status).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from src.core.models import ActionType, Decision, Fill

if TYPE_CHECKING:
    from src.hl.order_placer import HLOrderResponse


@dataclass
class Order:
    """Order record — paper mode fields plus HL Phase 4 exchange-tracking fields."""
    action:      ActionType
    symbol:      str
    qty:        float
    order_type:  str
    limit_price: Optional[float]
    status:      str
    created_at:  datetime
    # HL Phase 4: exchange-side tracking (all optional for paper-mode compat)
    cloid:             Optional[str]   = None
    hl_oid:            Optional[int]   = None
    exchange_status:   str             = "pending"
    filled_qty:        float           = 0.0
    avg_fill_px:       Optional[float] = None


def build_order(decision: Decision, symbol: str,
                order_type: str = "market",
                limit_price: Optional[float] = None) -> Order:
    return Order(
        action=decision.action,
        symbol=symbol,
        qty=decision.qty,
        order_type=order_type,
        limit_price=limit_price,
        status="pending",
        created_at=datetime.now(),
    )


def mark_filled(order: Order, fill: Fill) -> Order:
    order.status = "filled"
    return order


def mark_filled_hl(order: Order, response: "HLOrderResponse") -> Order:
    """Apply a Hyperliquid fill response to an Order record."""
    order.exchange_status   = "filled"
    order.hl_oid            = response.hl_oid
    order.filled_qty        = response.filled_sz
    order.avg_fill_px       = response.avg_fill_px
    return order


def mark_canceled(order: Order) -> Order:
    """Mark an order as canceled (e.g. after cancel_order succeeds)."""
    order.exchange_status = "canceled"
    return order
