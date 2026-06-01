from __future__ import annotations

from datetime import date, datetime, timezone
from itertools import combinations
from typing import Any, Iterable, List, Optional, Tuple

from src.core.models import (
    ActionType,
    EPSILON,
    BotConfig,
    BotState,
    Fill,
    LotRecord,
    PendingOrder,
    ReconciliationResult,
    ReconciliationStatus,
    ThesisState,
)
from src.execution.lifecycle import EMERGENCY_EXIT_ACTIONS, OPENING_ACTIONS, RISK_REDUCING_ACTIONS
from src.execution.operator_recovery import pending_action_to_action_type
from src.execution.utils import compute_realized_pnl_lifo
from src.hl.account import HLAccountSnapshot, HLFill, HLOpenOrder
from src.reporting.state_writer import apply_fill
from src.reporting.state_writer import mark_to_market_state


def _coin(config: BotConfig) -> str:
    if config.hl:
        return str(config.hl.get("coin") or config.symbol.get("ticker", "BTC"))
    return str(config.symbol.get("ticker", "BTC"))


def _is_flat_local(state: ThesisState) -> bool:
    return state.current_position_qty <= EPSILON and state.base_lot is None and not state.addon_lots


def _halt(
    state: ThesisState,
    reason: str,
    *,
    exchange_qty: float,
    open_orders_count: int,
) -> ReconciliationResult:
    state.state = BotState.HALTED
    state.halted = True
    state.halt_reason = reason
    return ReconciliationResult(
        status=ReconciliationStatus.HALTED,
        reason=reason,
        exchange_position_qty=exchange_qty,
        local_position_qty=state.current_position_qty,
        open_orders_count=open_orders_count,
        halt_reason=reason,
        applied_state_after=state.state.value,
    )


def _order_side_is_buy(order: HLOpenOrder) -> bool:
    return str(order.side).lower() in {"b", "buy"}


def _fill_is_buy_open(fill: HLFill) -> bool:
    side = str(fill.side).lower()
    direction = str(fill.direction or "").lower()
    return side in {"b", "buy"} or "open long" in direction or direction == "buy"


def _fill_is_sell_close(fill: HLFill) -> bool:
    side = str(fill.side).lower()
    direction = str(fill.direction or "").lower()
    return side in {"a", "ask", "sell"} or "close long" in direction or direction == "sell"


def _fill_time(fill: HLFill) -> int:
    return int(fill.timestamp_ms or 0)


def _fill_date(fill: HLFill) -> date:
    if fill.timestamp_ms is None:
        return datetime.now(timezone.utc).date()
    return datetime.fromtimestamp(fill.timestamp_ms / 1000, tz=timezone.utc).date()


def _coerce_fill(fill: HLFill | dict[str, Any]) -> Optional[HLFill]:
    if isinstance(fill, HLFill):
        return fill
    if not isinstance(fill, dict):
        return None
    try:
        raw_oid = fill.get("oid")
        raw_time = fill.get("time") or fill.get("timestamp")
        return HLFill(
            coin=str(fill.get("coin") or ""),
            side=str(fill.get("side") or fill.get("dir") or ""),
            qty=float(fill.get("sz") or fill.get("qty") or fill.get("size") or 0),
            price=float(fill.get("px") or fill.get("price") or 0),
            timestamp_ms=int(raw_time) if raw_time is not None else None,
            oid=int(raw_oid) if raw_oid is not None else None,
            direction=str(fill.get("dir") or "") or None,
        )
    except (TypeError, ValueError):
        return None


def _matching_subset_count(fills: List[HLFill], target_qty: float) -> int:
    # This detects ambiguous small fixture cases. For large live fill histories,
    # requiring the newest-prefix match keeps runtime bounded and conservative.
    if len(fills) > 16:
        return 1
    count = 0
    for size in range(1, len(fills) + 1):
        for combo in combinations(fills, size):
            qty = sum(f.qty for f in combo)
            if abs(qty - target_qty) <= EPSILON:
                count += 1
                if count > 1:
                    return count
    return count


def _match_recent_opening_fills(
    fills: Iterable[HLFill],
    *,
    coin: str,
    target_qty: float,
    pending_order: Optional[PendingOrder],
) -> Optional[List[HLFill]]:
    candidates = [
        f for f in fills
        if f.coin == coin
        and f.qty > EPSILON
        and f.price > 0
        and _fill_is_buy_open(f)
        and (pending_order is None or pending_order.oid is None or f.oid == pending_order.oid)
    ]
    candidates.sort(key=_fill_time, reverse=True)
    if not candidates:
        return None

    selected: List[HLFill] = []
    cumulative = 0.0
    for fill in candidates:
        selected.append(fill)
        cumulative += fill.qty
        if abs(cumulative - target_qty) <= EPSILON:
            break
        if cumulative > target_qty + EPSILON:
            return None
    if abs(cumulative - target_qty) > EPSILON:
        return None

    if _matching_subset_count(candidates, target_qty) > 1:
        return None
    return selected


def _match_recent_reducing_fills(
    fills: Iterable[HLFill],
    *,
    coin: str,
    target_qty: float,
    pending_order: PendingOrder,
) -> Optional[List[HLFill]]:
    candidates = [
        f for f in fills
        if f.coin == coin
        and f.qty > EPSILON
        and f.price > 0
        and _fill_is_sell_close(f)
        and (pending_order.oid is None or f.oid == pending_order.oid)
    ]
    candidates.sort(key=_fill_time, reverse=True)
    if not candidates:
        return None

    selected: List[HLFill] = []
    cumulative = 0.0
    for fill in candidates:
        selected.append(fill)
        cumulative += fill.qty
        if abs(cumulative - target_qty) <= EPSILON:
            break
        if cumulative > target_qty + EPSILON:
            return None
    if abs(cumulative - target_qty) > EPSILON:
        return None
    if _matching_subset_count(candidates, target_qty) > 1:
        return None
    return selected


def _has_pending_open_fill_evidence(
    fills: Iterable[HLFill],
    *,
    coin: str,
    pending_order: PendingOrder,
    local_qty: float,
    exchange_qty: float,
) -> bool:
    if abs(local_qty - exchange_qty) > EPSILON:
        return False
    matched = _match_recent_opening_fills(
        fills,
        coin=coin,
        target_qty=local_qty,
        pending_order=pending_order,
    )
    return matched is not None


def _reconstruct_position_from_fills(
    state: ThesisState,
    *,
    fills: List[HLFill],
    exchange_qty: float,
    mark_price: float,
) -> tuple[float, float]:
    weighted_notional = sum(fill.qty * fill.price for fill in fills)
    avg_entry = weighted_notional / exchange_qty
    entry_date = min(_fill_date(fill) for fill in fills)
    state.state = BotState.STARTER_LONG
    state.halted = False
    state.halt_reason = None
    state.base_lot = LotRecord(
        lot_id="base",
        entry_price=avg_entry,
        qty=exchange_qty,
        entry_date=entry_date,
    )
    state.addon_lots = []
    state.current_position_qty = exchange_qty
    state.original_base_qty = exchange_qty
    state.avg_entry_price = avg_entry
    state.entry_date = entry_date
    state.last_add_price = avg_entry
    state.pending_order = None
    mark_to_market_state(state, mark_price)
    return exchange_qty, avg_entry


def _apply_reducing_fills(
    state: ThesisState,
    *,
    fills: List[HLFill],
    action: ActionType,
    mark_price: float,
    intended_state_after: Optional[str],
) -> float:
    total_sold = 0.0
    for fill in sorted(fills, key=_fill_time):
        realized = compute_realized_pnl_lifo(state, fill.qty, fill.price)
        apply_fill(
            state,
            Fill(
                action=action,
                qty=fill.qty,
                fill_price=fill.price,
                slippage_bps=0.0,
                realized_pnl=realized,
                commission=0.0,
                timestamp=(
                    datetime.fromtimestamp(fill.timestamp_ms / 1000, tz=timezone.utc)
                    if fill.timestamp_ms is not None
                    else datetime.now(timezone.utc)
                ),
                order_id=fill.oid,
                order_status="filled",
            ),
        )
        total_sold += fill.qty
    state.pending_order = None
    if intended_state_after in {BotState.EXITED.value, BotState.HALTED.value} and state.current_position_qty <= EPSILON:
        state.state = BotState(intended_state_after)
        if state.state == BotState.HALTED:
            state.halted = True
            state.halt_reason = state.halt_reason or "reconciled_exit_halt"
        elif state.state == BotState.EXITED:
            state.halted = False
            state.halt_reason = None
    elif intended_state_after in {
        BotState.BASE_LONG.value,
        BotState.STARTER_LONG.value,
        BotState.PYRAMID_LONG.value,
        BotState.REDUCE_MODE.value,
    }:
        if not state.addon_lots and state.current_position_qty > EPSILON:
            state.state = BotState(intended_state_after)
            state.halted = False
            state.halt_reason = None
        elif state.addon_lots:
            state.state = BotState.PYRAMID_LONG
            state.halted = False
            state.halt_reason = None
    mark_to_market_state(state, mark_price)
    return total_sold


def _refresh_pending_order_from_exchange(
    state: ThesisState,
    *,
    order: HLOpenOrder,
    coin: str,
) -> PendingOrder:
    now = datetime.now(timezone.utc)
    prior = state.pending_order
    pending = PendingOrder(
        oid=order.oid,
        client_order_id=prior.client_order_id if prior is not None else None,
        symbol=coin,
        action=prior.action if prior is not None else "buy_starter",
        side="buy" if _order_side_is_buy(order) else "sell",
        reduce_only=prior.reduce_only if prior is not None else False,
        order_type=prior.order_type if prior is not None else "limit",
        qty=order.qty,
        qty_submitted=prior.qty_submitted if prior is not None and prior.qty_submitted > EPSILON else order.qty,
        qty_filled=prior.qty_filled if prior is not None else 0.0,
        qty_remaining=order.qty,
        limit_px=order.limit_px,
        status="submitted_unfilled",
        created_at=prior.created_at if prior is not None else now,
        last_checked_at=now,
        source_decision_id=prior.source_decision_id if prior is not None else None,
        state_before=prior.state_before if prior is not None else state.state.value,
        intended_state_after=prior.intended_state_after if prior is not None else BotState.STARTER_LONG.value,
        applied_state_after=state.state.value,
    )
    state.pending_order = pending
    return pending


def reconcile_state_with_exchange(
    state: ThesisState,
    snapshot: HLAccountSnapshot,
    recent_fills: List[HLFill] | List[dict[str, Any]],
    config: BotConfig,
) -> Tuple[ThesisState, ReconciliationResult]:
    coin = _coin(config)
    open_orders = [order for order in (snapshot.open_orders or []) if order.coin == coin]
    exchange_pos = next((pos for pos in snapshot.positions if pos.coin == coin), None)
    exchange_qty = exchange_pos.qty if exchange_pos is not None else 0.0
    mark_price = exchange_pos.mark_price if exchange_pos is not None else 0.0
    local_qty = state.current_position_qty

    unexpected_positions = [
        pos for pos in snapshot.positions
        if pos.coin != coin and abs(pos.qty) > EPSILON
    ]
    if unexpected_positions:
        return state, _halt(
            state,
            "unexpected_non_bot_position",
            exchange_qty=exchange_qty,
            open_orders_count=len(open_orders),
        )

    if exchange_qty < -EPSILON:
        return state, _halt(
            state,
            "unexpected_short_position",
            exchange_qty=exchange_qty,
            open_orders_count=len(open_orders),
        )

    if len(open_orders) > 1:
        return state, _halt(
            state,
            "multiple_open_orders_not_supported",
            exchange_qty=exchange_qty,
            open_orders_count=len(open_orders),
        )

    if open_orders and exchange_qty <= EPSILON and _is_flat_local(state):
        _refresh_pending_order_from_exchange(state, order=open_orders[0], coin=coin)
        return state, ReconciliationResult(
            status=ReconciliationStatus.REFRESHED_PENDING_ORDER,
            reason="existing_open_order",
            exchange_position_qty=exchange_qty,
            local_position_qty=local_qty,
            open_orders_count=len(open_orders),
            pending_order_updated=True,
            applied_state_after=state.state.value,
        )

    typed_fills = [fill for fill in (_coerce_fill(fill) for fill in recent_fills) if fill is not None]

    if _is_flat_local(state) and exchange_qty <= EPSILON and not open_orders:
        if state.pending_order is not None:
            return state, _halt(
                state,
                "pending_order_missing_on_exchange",
                exchange_qty=exchange_qty,
                open_orders_count=0,
            )
        return state, ReconciliationResult(
            status=ReconciliationStatus.OK,
            reason="flat_in_sync",
            exchange_position_qty=exchange_qty,
            local_position_qty=local_qty,
            open_orders_count=0,
            applied_state_after=state.state.value,
        )

    if _is_flat_local(state) and exchange_qty > EPSILON and not open_orders:
        matched = _match_recent_opening_fills(
            typed_fills,
            coin=coin,
            target_qty=exchange_qty,
            pending_order=state.pending_order,
        )
        if not matched:
            return state, _halt(
                state,
                "position_without_reconstructable_fill",
                exchange_qty=exchange_qty,
                open_orders_count=0,
            )
        reconstruction_mark_price = mark_price or matched[0].price
        reconstructed_qty, avg_entry = _reconstruct_position_from_fills(
            state,
            fills=matched,
            exchange_qty=exchange_qty,
            mark_price=reconstruction_mark_price,
        )
        return state, ReconciliationResult(
            status=ReconciliationStatus.RECONSTRUCTED_POSITION,
            reason="reconstructed_from_recent_fills",
            exchange_position_qty=exchange_qty,
            local_position_qty=local_qty,
            open_orders_count=0,
            matched_fill_count=len(matched),
            reconstructed_avg_entry_price=avg_entry,
            reconstructed_qty=reconstructed_qty,
            pending_order_updated=False,
            applied_state_after=state.state.value,
        )

    if state.current_position_qty > EPSILON:
        if state.pending_order is not None and not open_orders:
            pending_action = pending_action_to_action_type(state.pending_order.action)
            if pending_action is None:
                return state, _halt(
                    state,
                    "pending_order_unknown_action",
                    exchange_qty=exchange_qty,
                    open_orders_count=0,
                )
            if pending_action in OPENING_ACTIONS:
                if _has_pending_open_fill_evidence(
                    typed_fills,
                    coin=coin,
                    pending_order=state.pending_order,
                    local_qty=state.current_position_qty,
                    exchange_qty=max(exchange_qty, 0.0),
                ):
                    state.pending_order = None
                    mark_to_market_state(state, mark_price or state.avg_entry_price or 0.0)
                    return state, ReconciliationResult(
                        status=ReconciliationStatus.CLEARED_PENDING_ORDER,
                        reason="cleared_filled_opening_pending_order",
                        exchange_position_qty=exchange_qty,
                        local_position_qty=local_qty,
                        open_orders_count=0,
                        matched_fill_count=1,
                        pending_order_updated=True,
                        applied_state_after=state.state.value,
                    )
                return state, _halt(
                    state,
                    "pending_order_missing_on_exchange",
                    exchange_qty=exchange_qty,
                    open_orders_count=0,
                )
            if pending_action in (RISK_REDUCING_ACTIONS | EMERGENCY_EXIT_ACTIONS):
                sold_qty = state.current_position_qty - max(exchange_qty, 0.0)
                if sold_qty <= EPSILON:
                    return state, _halt(
                        state,
                        "pending_reduce_missing_on_exchange",
                        exchange_qty=exchange_qty,
                        open_orders_count=0,
                    )
                matched = _match_recent_reducing_fills(
                    typed_fills,
                    coin=coin,
                    target_qty=sold_qty,
                    pending_order=state.pending_order,
                )
                if not matched:
                    return state, _halt(
                        state,
                        (
                            "pending_exit_missing_on_exchange"
                            if pending_action in EMERGENCY_EXIT_ACTIONS
                            else "pending_reduce_missing_on_exchange"
                        ),
                        exchange_qty=exchange_qty,
                        open_orders_count=0,
                    )
                _apply_reducing_fills(
                    state,
                    fills=matched,
                    action=pending_action,
                    mark_price=mark_price or matched[-1].price,
                    intended_state_after=state.pending_order.intended_state_after,
                )
                if abs(state.current_position_qty - max(exchange_qty, 0.0)) > EPSILON:
                    return state, _halt(
                        state,
                        "local_exchange_position_mismatch",
                        exchange_qty=exchange_qty,
                        open_orders_count=0,
                    )
                return state, ReconciliationResult(
                    status=ReconciliationStatus.CLEARED_PENDING_ORDER,
                    reason="reconciled_pending_reduce_from_recent_fills",
                    exchange_position_qty=exchange_qty,
                    local_position_qty=local_qty,
                    open_orders_count=0,
                    matched_fill_count=len(matched),
                    reconstructed_qty=state.current_position_qty,
                    pending_order_updated=True,
                    applied_state_after=state.state.value,
                )

        if exchange_qty <= EPSILON and not open_orders:
            return state, _halt(
                state,
                "local_exchange_position_mismatch",
                exchange_qty=exchange_qty,
                open_orders_count=0,
            )
        if abs(exchange_qty - state.current_position_qty) > EPSILON:
            return state, _halt(
                state,
                "local_exchange_position_mismatch",
                exchange_qty=exchange_qty,
                open_orders_count=len(open_orders),
            )
        if state.pending_order is not None and open_orders:
            _refresh_pending_order_from_exchange(state, order=open_orders[0], coin=coin)
            return state, ReconciliationResult(
                status=ReconciliationStatus.REFRESHED_PENDING_ORDER,
                reason="pending_order_still_open",
                exchange_position_qty=exchange_qty,
                local_position_qty=local_qty,
                open_orders_count=len(open_orders),
                pending_order_updated=True,
                applied_state_after=state.state.value,
            )
        return state, ReconciliationResult(
            status=ReconciliationStatus.OK,
            reason="position_in_sync",
            exchange_position_qty=exchange_qty,
            local_position_qty=local_qty,
            open_orders_count=len(open_orders),
            applied_state_after=state.state.value,
        )

    return state, _halt(
        state,
        "reconciliation_unsupported_state",
        exchange_qty=exchange_qty,
        open_orders_count=len(open_orders),
    )
