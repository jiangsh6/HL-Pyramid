from __future__ import annotations

from src.core.models import ActionClass, ActionType, EPSILON, OrderResult, OrderResultStatus


OPENING_ACTIONS = frozenset({
    ActionType.BUY_STARTER,
    ActionType.BUY_BASE,
})

ADDING_ACTIONS = frozenset({
    ActionType.BUY_ADDON,
})

RISK_REDUCING_ACTIONS = frozenset({
    ActionType.SELL_REDUCE_ADDON,
    ActionType.SELL_REDUCE_BASE,
    ActionType.SELL_TAKE_PROFIT,
    ActionType.SELL_EVENT_DERISKING,
})

EMERGENCY_EXIT_ACTIONS = frozenset({
    ActionType.SELL_STOP,
    ActionType.SELL_TRAILING_STOP,
    ActionType.EXIT_ALL,
})


def classify_action(action: ActionType) -> ActionClass:
    if action in OPENING_ACTIONS:
        return ActionClass.OPENING
    if action in ADDING_ACTIONS:
        return ActionClass.ADDING
    if action in RISK_REDUCING_ACTIONS:
        return ActionClass.RISK_REDUCING
    if action in EMERGENCY_EXIT_ACTIONS:
        return ActionClass.EMERGENCY_EXIT
    if action in {ActionType.NO_ACTION, ActionType.HALT, ActionType.RECONCILE_POSITION}:
        return ActionClass.NON_ORDER
    return ActionClass.NON_ORDER


def is_order_action(action: ActionType) -> bool:
    return classify_action(action) not in {ActionClass.NON_ORDER, ActionClass.CANCEL}


def is_terminal_execution_failure(order_result: OrderResult) -> bool:
    return order_result.status in {
        OrderResultStatus.EXECUTION_ERROR,
        OrderResultStatus.UNKNOWN,
        OrderResultStatus.CANCEL_FAILED,
    }


def is_unfilled_emergency_exit(order_result: OrderResult) -> bool:
    return (
        classify_action(order_result.action) == ActionClass.EMERGENCY_EXIT
        and order_result.status == OrderResultStatus.SUBMITTED_UNFILLED
        and order_result.filled_qty <= EPSILON
    )


def is_residual_emergency_exit(order_result: OrderResult) -> bool:
    return (
        classify_action(order_result.action) == ActionClass.EMERGENCY_EXIT
        and order_result.filled_qty > EPSILON
        and order_result.remaining_qty > EPSILON
    )
