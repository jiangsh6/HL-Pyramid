from __future__ import annotations

from datetime import datetime, timezone

from scripts.flatten_hl_position import (
    RECOVERABLE_CLEANUP_EXIT_HALT_PREFIX,
    can_recover_cleanup_exit_halt,
)
from src.core.models import BotState, PendingOrder, ThesisState


def _halted_state(**overrides) -> ThesisState:
    values = {
        "symbol": "BTC",
        "state": BotState.HALTED,
        "halted": True,
        "halt_reason": RECOVERABLE_CLEANUP_EXIT_HALT_PREFIX + " asset=3",
    }
    values.update(overrides)
    return ThesisState(**values)


def _pending_order() -> PendingOrder:
    return PendingOrder(
        oid=123,
        symbol="BTC",
        action="exit_all",
        side="sell",
        reduce_only=True,
        qty=0.00125,
        qty_submitted=0.00125,
        qty_filled=0.0,
        qty_remaining=0.00125,
        limit_px=71700.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
    )


def test_recoverable_cleanup_price_halt_allows_explicit_testnet_retry():
    state = _halted_state()

    assert can_recover_cleanup_exit_halt(
        state=state,
        network="testnet",
        recover_flag=True,
        confirmed=True,
        open_orders_count=0,
    )


def test_recoverable_cleanup_price_halt_requires_flag():
    state = _halted_state()

    assert not can_recover_cleanup_exit_halt(
        state=state,
        network="testnet",
        recover_flag=False,
        confirmed=True,
        open_orders_count=0,
    )


def test_recoverable_cleanup_price_halt_requires_confirm():
    state = _halted_state()

    assert not can_recover_cleanup_exit_halt(
        state=state,
        network="testnet",
        recover_flag=True,
        confirmed=False,
        open_orders_count=0,
    )


def test_recoverable_cleanup_price_halt_refuses_non_recoverable_halt():
    state = _halted_state(halt_reason="local_exchange_position_mismatch")

    assert not can_recover_cleanup_exit_halt(
        state=state,
        network="testnet",
        recover_flag=True,
        confirmed=True,
        open_orders_count=0,
    )


def test_recoverable_cleanup_price_halt_refuses_pending_order():
    state = _halted_state(pending_order=_pending_order())

    assert not can_recover_cleanup_exit_halt(
        state=state,
        network="testnet",
        recover_flag=True,
        confirmed=True,
        open_orders_count=0,
    )


def test_recoverable_cleanup_price_halt_refuses_open_orders():
    state = _halted_state()

    assert not can_recover_cleanup_exit_halt(
        state=state,
        network="testnet",
        recover_flag=True,
        confirmed=True,
        open_orders_count=1,
    )


def test_recoverable_cleanup_price_halt_refuses_mainnet():
    state = _halted_state()

    assert not can_recover_cleanup_exit_halt(
        state=state,
        network="mainnet",
        recover_flag=True,
        confirmed=True,
        open_orders_count=0,
    )

