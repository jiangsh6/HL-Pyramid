from __future__ import annotations

from typing import Optional

from src.core.models import ActionType, PendingOrder


DUST_EXIT_PENDING_ACTION = "exit_dust"


def pending_action_to_action_type(action: str) -> Optional[ActionType]:
    """Map persisted operator-recovery pending actions to fill actions."""
    if action == DUST_EXIT_PENDING_ACTION:
        return ActionType.EXIT_ALL
    try:
        return ActionType(action)
    except ValueError:
        return None


def pending_missing_reason(pending: PendingOrder) -> str:
    action = pending.action
    if action in {"buy_starter", "buy_base", "buy_addon"}:
        return "pending_entry_missing_on_exchange"
    if action == DUST_EXIT_PENDING_ACTION:
        return "pending_dust_exit_missing_on_exchange"
    if action in {"exit_all"}:
        return "pending_exit_missing_on_exchange"
    return "pending_order_missing_on_exchange"


def pending_reconcile_result_text(pending: PendingOrder) -> str:
    action = pending.action
    if action in {"buy_starter", "buy_base", "buy_addon"}:
        return "pending_entry_filled_reconciled"
    if action == DUST_EXIT_PENDING_ACTION:
        return "pending_dust_exit_filled_reconciled"
    if action in {"sell_reduce_addon", "sell_reduce_base", "sell_take_profit", "sell_event_derisking"}:
        return "pending_reduce_filled_reconciled"
    return "pending_exit_filled_reconciled"


def runner_target_from_pending(pending: PendingOrder) -> Optional[float]:
    source = pending.source_decision_id or ""
    prefix = "target_runner:"
    if not source.startswith(prefix):
        return None
    try:
        return float(source[len(prefix):])
    except ValueError:
        return None


def is_target_runner_pending(pending: PendingOrder) -> bool:
    return runner_target_from_pending(pending) is not None
