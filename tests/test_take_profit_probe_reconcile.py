from __future__ import annotations

from datetime import datetime, timezone

from scripts.cancel_hl_order import _tp_level_from_pending
from src.core.models import PendingOrder


def _pending(source_decision_id: str | None) -> PendingOrder:
    return PendingOrder(
        oid=123,
        symbol="BTC",
        action="sell_take_profit",
        side="sell",
        reduce_only=True,
        qty=0.001,
        qty_submitted=0.001,
        qty_filled=0.0,
        qty_remaining=0.001,
        limit_px=71846.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
        source_decision_id=source_decision_id,
    )


def test_tp_level_source_decision_id_is_parsed_for_reconciliation():
    assert _tp_level_from_pending(_pending("tp_level:0")) == 0
    assert _tp_level_from_pending(_pending("tp_level:3")) == 3


def test_tp_level_source_decision_id_rejects_unknown_or_out_of_range_values():
    assert _tp_level_from_pending(_pending(None)) is None
    assert _tp_level_from_pending(_pending("tp_level:4")) is None
    assert _tp_level_from_pending(_pending("tp_level:not-an-int")) is None
    assert _tp_level_from_pending(_pending("other:0")) is None

