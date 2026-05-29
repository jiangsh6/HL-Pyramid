"""
Order lifecycle helpers.  In v0.1 paper mode, fills are immediate; no real
order state machine is needed.  These helpers exist to support
run_daily_decision and the backtester, and to mirror live broker semantics
without exposing live-trading code paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.core.models import ActionType, Decision, Fill


@dataclass
class Order:
    """Lightweight order record (paper mode — no broker IDs)."""
    action:      ActionType
    symbol:      str
    shares:      int
    order_type:  str
    limit_price: Optional[float]
    status:      str
    created_at:  datetime


def build_order(decision: Decision, symbol: str,
                order_type: str = "market",
                limit_price: Optional[float] = None) -> Order:
    return Order(
        action=decision.action,
        symbol=symbol,
        shares=decision.shares,
        order_type=order_type,
        limit_price=limit_price,
        status="pending",
        created_at=datetime.now(),
    )


def mark_filled(order: Order, fill: Fill) -> Order:
    order.status = "filled"
    return order
