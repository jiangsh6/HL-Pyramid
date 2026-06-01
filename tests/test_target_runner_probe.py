from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import Mock

from scripts.probe_hl_target_runner import (
    apply_target_runner_order_result,
    compute_runner_quantities,
    get_live_probe_prices,
    resolve_probe_target_runner_pricing,
    validate_halted_target_runner_recovery,
)
from src.core.config_loader import load_config
from src.core.models import ActionType, BotState, Decision, LotRecord, OrderResult, OrderResultStatus, PendingOrder, ThesisState
from src.execution.operator_recovery import is_target_runner_pending, runner_target_from_pending


def _state(*, original_base_qty=0.002, base_qty=0.002, current_qty=0.002, state_value=BotState.BASE_LONG) -> ThesisState:
    state = ThesisState(symbol="BTC", state=state_value)
    state.base_lot = LotRecord(
        lot_id="base",
        entry_price=72000.0,
        qty=base_qty,
        entry_date=date(2026, 5, 31),
    )
    state.original_base_qty = original_base_qty
    state.current_position_qty = current_qty
    state.avg_entry_price = 72000.0
    return state


def _recovery_state(**overrides) -> ThesisState:
    state = _state(
        original_base_qty=0.005,
        base_qty=0.005,
        current_qty=0.005,
        state_value=BotState.HALTED,
    )
    state.halted = True
    state.halt_reason = "exit_order_canceled_position_still_open"
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


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
        limit_px=72000.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
        source_decision_id=source_decision_id,
    )


def _decision(qty: float = 0.0025) -> Decision:
    return Decision(
        action=ActionType.SELL_TAKE_PROFIT,
        qty=qty,
        reason="test_target_runner",
        new_state=BotState.RUNNER_LONG,
    )


def _order_result(status: OrderResultStatus, *, filled_qty=0.0, submitted_qty=0.0025) -> OrderResult:
    return OrderResult(
        status=status,
        action=ActionType.SELL_TAKE_PROFIT,
        side="sell",
        reduce_only=True,
        submitted_qty=submitted_qty,
        filled_qty=filled_qty,
        remaining_qty=max(submitted_qty - filled_qty, 0.0),
        avg_fill_px=72380.0 if filled_qty > 0 else None,
        limit_px=72394.0,
        state_before=BotState.HALTED.value,
        intended_state_after=BotState.RUNNER_LONG.value,
        applied_state_after=BotState.HALTED.value,
        created_at=datetime.now(timezone.utc),
    )


def test_runner_target_uses_original_base_qty_not_reduced_base_lot_qty():
    state = _state(original_base_qty=0.002, base_qty=0.001, current_qty=0.002)

    runner_target, reduce_qty, blocker = compute_runner_quantities(state, 0.5, 0.001)

    assert blocker == "target_runner_qty_below_realistic_threshold"
    assert runner_target == 0.001
    assert reduce_qty == 0.001


def test_runner_quantity_blocks_reduce_below_min_size():
    state = _state(original_base_qty=0.002, base_qty=0.002, current_qty=0.0015)

    runner_target, reduce_qty, blocker = compute_runner_quantities(state, 0.5, 0.001)

    assert runner_target == 0.001
    assert reduce_qty == 0.0005
    assert blocker == "target_runner_reduce_qty_below_min_size"


def test_runner_quantity_blocks_runner_residual_below_min_size():
    state = _state(original_base_qty=0.0015, base_qty=0.0015, current_qty=0.002)

    runner_target, reduce_qty, blocker = compute_runner_quantities(state, 0.5, 0.001)

    assert runner_target == 0.00075
    assert reduce_qty == 0.00125
    assert blocker == "target_runner_qty_below_min_size"


def test_target_runner_pending_metadata_parses_runner_target():
    pending = _pending("target_runner:0.001")

    assert is_target_runner_pending(pending) is True
    assert runner_target_from_pending(pending) == 0.001


def test_target_runner_pending_metadata_rejects_unknown_values():
    assert runner_target_from_pending(_pending(None)) is None
    assert runner_target_from_pending(_pending("target_runner:not-a-float")) is None
    assert is_target_runner_pending(_pending("tp_level:0")) is False


def test_realistic_runner_reduce_qty_passes_probe_validation():
    state = _state(original_base_qty=0.005, base_qty=0.005, current_qty=0.005)

    runner_target, reduce_qty, blocker = compute_runner_quantities(state, 0.5, 0.001)

    assert blocker is None
    assert runner_target == 0.0025
    assert reduce_qty == 0.0025


def test_halted_recovery_guard_rejects_wrong_halt_reason():
    state = _recovery_state(halt_reason="other")

    blocker = validate_halted_target_runner_recovery(
        state,
        exchange_qty=0.005,
        open_orders_count=0,
        reduce_qty=0.0025,
        min_size=0.001,
    )

    assert blocker == "recovery_wrong_halt_reason"


def test_halted_recovery_guard_rejects_pending_order_not_null():
    state = _recovery_state(pending_order=_pending("target_runner:0.0025"))

    blocker = validate_halted_target_runner_recovery(
        state,
        exchange_qty=0.005,
        open_orders_count=0,
        reduce_qty=0.0025,
        min_size=0.001,
    )

    assert blocker == "recovery_pending_order_exists"


def test_halted_recovery_guard_rejects_open_orders():
    blocker = validate_halted_target_runner_recovery(
        _recovery_state(),
        exchange_qty=0.005,
        open_orders_count=1,
        reduce_qty=0.0025,
        min_size=0.001,
    )

    assert blocker == "recovery_open_orders_count=1"


def test_halted_recovery_guard_rejects_exchange_qty_zero():
    blocker = validate_halted_target_runner_recovery(
        _recovery_state(),
        exchange_qty=0.0,
        open_orders_count=0,
        reduce_qty=0.0025,
        min_size=0.001,
    )

    assert blocker == "recovery_no_exchange_position"


def test_halted_recovery_guard_rejects_local_qty_zero():
    blocker = validate_halted_target_runner_recovery(
        _recovery_state(current_position_qty=0.0),
        exchange_qty=0.005,
        open_orders_count=0,
        reduce_qty=0.0025,
        min_size=0.001,
    )

    assert blocker == "recovery_no_local_position"


def test_halted_recovery_guard_rejects_target_price_tp_triggered_true():
    blocker = validate_halted_target_runner_recovery(
        _recovery_state(target_price_tp_triggered=True),
        exchange_qty=0.005,
        open_orders_count=0,
        reduce_qty=0.0025,
        min_size=0.001,
    )

    assert blocker == "recovery_target_price_already_triggered"


def test_halted_recovery_guard_rejects_runner_mode_active_true():
    blocker = validate_halted_target_runner_recovery(
        _recovery_state(runner_mode_active=True),
        exchange_qty=0.005,
        open_orders_count=0,
        reduce_qty=0.0025,
        min_size=0.001,
    )

    assert blocker == "recovery_runner_mode_already_active"


def test_halted_recovery_guard_accepts_known_safe_state():
    blocker = validate_halted_target_runner_recovery(
        _recovery_state(),
        exchange_qty=0.005,
        open_orders_count=0,
        reduce_qty=0.0025,
        min_size=0.001,
    )

    assert blocker is None


def test_live_price_helper_extracts_mark_and_oracle():
    client = Mock()
    client.post_info.return_value = [
        {"universe": [{"name": "ETH"}, {"name": "BTC"}]},
        [{"markPx": "3000", "oraclePx": "3001"}, {"markPx": "73123.4", "oraclePx": "73120.1"}],
    ]

    mark_px, oracle_px = get_live_probe_prices(client, "BTC")

    assert mark_px == 73123.4
    assert oracle_px == 73120.1
    client.post_info.assert_called_once_with({"type": "metaAndAssetCtxs"})


def test_live_price_helper_falls_back_to_oracle_when_mark_missing():
    client = Mock()
    client.post_info.return_value = [
        {"universe": [{"name": "BTC"}]},
        [{"oraclePx": "73120.1"}],
    ]

    mark_px, oracle_px = get_live_probe_prices(client, "BTC")

    assert mark_px is None
    assert oracle_px == 73120.1


def test_live_price_helper_fails_safely_when_mark_and_oracle_missing():
    client = Mock()
    client.post_info.return_value = [
        {"universe": [{"name": "BTC"}]},
        [{}],
    ]

    mark_px, oracle_px = get_live_probe_prices(client, "BTC")

    assert mark_px is None
    assert oracle_px is None


def test_aggressive_sell_target_price_is_below_live_mark(monkeypatch):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0x0000000000000000000000000000000000000000")
    config = load_config("config/btc_testnet_realistic_probe.yaml")

    pricing = resolve_probe_target_runner_pricing(
        config=config,
        mark_price=72380.0,
        oracle_price=73748.0,
        sz_decimals=3,
        pricing_mode="aggressive",
        aggressiveness_bps=10,
        max_oracle_deviation_bps=20,
        max_aggressiveness_bps=20,
        time_in_force="ioc",
    )

    assert pricing["blocker"] is None
    assert pricing["raw_probe_px"] < 72380.0
    assert pricing["formatted_probe_px"] < 72380.0
    assert pricing["formatted_probe_px"] <= pricing["raw_probe_px"]
    assert pricing["probe_config"].execution["time_in_force"] == "ioc"


def test_cross_sell_target_price_respects_oracle_cap(monkeypatch):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0x0000000000000000000000000000000000000000")
    config = load_config("config/btc_testnet_realistic_probe.yaml")

    pricing = resolve_probe_target_runner_pricing(
        config=config,
        mark_price=72380.0,
        oracle_price=73748.0,
        sz_decimals=3,
        pricing_mode="cross",
        aggressiveness_bps=25,
        max_oracle_deviation_bps=20,
        max_aggressiveness_bps=30,
        time_in_force="ioc",
    )

    assert pricing["blocker"] == "target_runner_cross_exceeds_max_oracle_deviation"


def test_ioc_target_runner_unfilled_does_not_transition():
    state = _recovery_state()
    order_result = _order_result(OrderResultStatus.SUBMITTED_UNFILLED)

    fill, risk_status = apply_target_runner_order_result(
        state,
        _decision(),
        order_result,
        runner_target_qty=0.0025,
        min_size=0.001,
        is_ioc=True,
    )

    assert fill is None
    assert risk_status == "blocked"
    assert state.state == BotState.HALTED
    assert state.target_price_tp_triggered is False
    assert state.runner_mode_active is False
    assert state.pending_order is None


def test_ioc_target_runner_immediate_match_rejection_does_not_transition():
    state = _recovery_state()
    order_result = _order_result(OrderResultStatus.REJECTED)
    order_result.exchange_error_sanitized = "Order could not immediately match against any resting orders. asset=3"

    fill, risk_status = apply_target_runner_order_result(
        state,
        _decision(),
        order_result,
        runner_target_qty=0.0025,
        min_size=0.001,
        is_ioc=True,
    )

    assert fill is None
    assert risk_status == "blocked"
    assert state.state == BotState.HALTED
    assert state.target_price_tp_triggered is False
    assert state.runner_mode_active is False
    assert state.pending_order is None


def test_ioc_target_runner_partial_fill_applies_only_filled_qty():
    state = _recovery_state()
    order_result = _order_result(OrderResultStatus.PARTIALLY_FILLED, filled_qty=0.001)

    fill, risk_status = apply_target_runner_order_result(
        state,
        _decision(),
        order_result,
        runner_target_qty=0.0025,
        min_size=0.001,
        is_ioc=True,
    )

    assert fill is not None
    assert risk_status == "halted"
    assert state.current_position_qty == 0.004
    assert state.base_lot is not None
    assert state.base_lot.qty == 0.004
    assert state.state == BotState.HALTED
    assert state.target_price_tp_triggered is False
    assert state.runner_mode_active is False
    assert state.pending_order is None


def test_ioc_target_runner_full_fill_transitions_to_runner_long():
    state = _recovery_state()
    order_result = _order_result(OrderResultStatus.FILLED, filled_qty=0.0025)

    fill, risk_status = apply_target_runner_order_result(
        state,
        _decision(),
        order_result,
        runner_target_qty=0.0025,
        min_size=0.001,
        is_ioc=True,
    )

    assert fill is not None
    assert risk_status == "ok"
    assert state.current_position_qty == 0.0025
    assert state.base_lot is not None
    assert state.base_lot.qty == 0.0025
    assert state.state == BotState.RUNNER_LONG
    assert state.target_price_tp_triggered is True
    assert state.runner_mode_active is True
    assert state.pending_order is None
    assert state.halted is False
