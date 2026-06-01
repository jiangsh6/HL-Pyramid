from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from scripts.probe_hl_hard_stop import apply_hard_stop_order_result
from src.core.models import ActionType, BotState, Decision, LotRecord, OrderResult, OrderResultStatus, ThesisState


def _state(qty: float = 0.005) -> ThesisState:
    state = ThesisState(symbol="BTC", state=BotState.STARTER_LONG)
    state.base_lot = LotRecord(
        lot_id="base",
        entry_price=72000.0,
        qty=qty,
        entry_date=date(2026, 6, 1),
    )
    state.current_position_qty = qty
    state.original_base_qty = qty
    state.avg_entry_price = 72000.0
    state.sz_decimals = 5
    return state


def _decision(qty: float = 0.005) -> Decision:
    return Decision(
        action=ActionType.SELL_STOP,
        qty=qty,
        reason="hard_stop_triggered",
        new_state=BotState.EXITED,
    )


def _result(status: OrderResultStatus, *, filled_qty=0.0, submitted_qty=0.005, error: str | None = None) -> OrderResult:
    return OrderResult(
        status=status,
        action=ActionType.SELL_STOP,
        side="sell",
        reduce_only=True,
        submitted_qty=submitted_qty,
        filled_qty=filled_qty,
        remaining_qty=max(submitted_qty - filled_qty, 0.0),
        avg_fill_px=72000.0 if filled_qty > 0 else None,
        limit_px=71900.0,
        exchange_error_sanitized=error,
        state_before=BotState.STARTER_LONG.value,
        intended_state_after=BotState.EXITED.value,
        applied_state_after=BotState.STARTER_LONG.value,
        created_at=datetime.now(timezone.utc),
    )


def test_hard_stop_ioc_full_fill_exits():
    state = _state()
    order_result = _result(OrderResultStatus.FILLED, filled_qty=0.005)

    fill, risk_status = apply_hard_stop_order_result(state, _decision(), order_result, is_ioc=True)

    assert fill is not None
    assert risk_status == "ok"
    assert state.state == BotState.EXITED
    assert state.current_position_qty == 0.0
    assert state.pending_order is None
    assert state.halted is False


def test_hard_stop_ioc_unfilled_rejection_halts_without_pending():
    state = _state()
    order_result = _result(
        OrderResultStatus.REJECTED,
        error="Order could not immediately match against any resting orders. asset=3",
    )

    fill, risk_status = apply_hard_stop_order_result(state, _decision(), order_result, is_ioc=True)

    assert fill is None
    assert risk_status == "halted"
    assert state.state == BotState.HALTED
    assert state.current_position_qty == pytest.approx(0.005)
    assert state.pending_order is None
    assert state.halt_reason == "hard_stop_ioc_unfilled_position_still_open"


def test_hard_stop_ioc_partial_fill_applies_only_fill_and_halts_without_pending():
    state = _state()
    order_result = _result(OrderResultStatus.PARTIALLY_FILLED, filled_qty=0.002)

    fill, risk_status = apply_hard_stop_order_result(state, _decision(), order_result, is_ioc=True)

    assert fill is not None
    assert risk_status == "halted"
    assert state.state == BotState.HALTED
    assert state.current_position_qty == pytest.approx(0.003)
    assert state.pending_order is None
    assert state.halt_reason == "hard_stop_ioc_partial_residual"


def test_hard_stop_gtc_unfilled_keeps_active_state_with_pending_exit():
    state = _state()
    order_result = _result(OrderResultStatus.SUBMITTED_UNFILLED)

    fill, risk_status = apply_hard_stop_order_result(state, _decision(), order_result, is_ioc=False)

    assert fill is None
    assert risk_status == "pending_exit"
    assert state.state == BotState.STARTER_LONG
    assert state.current_position_qty == pytest.approx(0.005)
    assert state.pending_order is not None
    assert state.pending_order.action == ActionType.SELL_STOP.value
    assert state.halted is False
