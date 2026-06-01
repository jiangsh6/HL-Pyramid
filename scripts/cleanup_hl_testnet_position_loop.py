"""Supervised testnet-only loop for reducing an existing HL BTC position.

The loop is deliberately not a strategy loop. It only attempts bounded,
operator-confirmed, reduce-only IOC cleanup against an already aligned testnet
position.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.flatten_hl_position import _marketable_exit_reference_price
from src.core.config_loader import config_snapshot_hash, load_config
from src.core.models import (
    ActionType,
    BotState,
    Decision,
    EPSILON,
    Fill,
    IndicatorSnapshot,
    OrderResult,
    OrderResultStatus,
    PendingOrder,
)
from src.execution.hl_broker import execute_hl
from src.execution.reconciler import reconcile_state_with_exchange
from src.hl.account import HLAccountSnapshot, get_account_snapshot, get_recent_user_fills
from src.hl.auth import get_private_key
from src.hl.client import HyperliquidClient
from src.hl.order_placer import cancel_order
from src.reporting.logger import log_cycle
from src.reporting.state_writer import (
    apply_fill,
    load_state_or_halt,
    mark_to_market_state,
    write_state,
)


RECOVERABLE_CLEANUP_HALT_PREFIXES = (
    "cleanup_exit_failed:",
    "ioc_flatten_unfilled_position_still_open",
    "residual_position_after_ioc_emergency_exit",
    "exit_order_canceled_position_still_open",
)


def _mask(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr
    return addr[:6] + "..." + addr[-3:]


def _position_qty(snapshot: HLAccountSnapshot, coin: str) -> float:
    position = next((p for p in snapshot.positions if p.coin == coin), None)
    return position.qty if position is not None else 0.0


def _mark_price(snapshot: HLAccountSnapshot, coin: str, fallback: float = 1.0) -> float:
    position = next((p for p in snapshot.positions if p.coin == coin), None)
    if position is None:
        return fallback
    return position.mark_price or position.entry_price or fallback


def _synthetic_indicators(mark_price: float) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        date=datetime.now(timezone.utc).date(),
        adj_close=mark_price,
        open=mark_price,
        high=mark_price,
        low=mark_price,
        volume=0.0,
        prev_adj_close=mark_price,
        ma5=mark_price,
        ma10=mark_price,
        ma20=mark_price,
        ma50=mark_price,
        atr14=max(mark_price * 0.01, 1.0),
        avg_volume_20d=0.0,
        prior_highest_high_20d=mark_price,
        drawdown_from_20d_high=0.0,
        distance_from_ma10=0.0,
        distance_from_ma20=0.0,
        intraday_return=0.0,
        gap_up_pct=0.0,
        gap_down_pct=0.0,
        close=mark_price,
        bar_time=datetime.now(timezone.utc),
    )


def _log_cycle_safe(
    *,
    config,
    state,
    decision,
    indicators,
    equity: float,
    state_before: str,
    order_result: OrderResult | None = None,
    reconciliation_result=None,
    risk_status_override: str | None = None,
) -> None:
    log_cycle(
        run_dir=config.logging.get("run_dir", "reports/btc_hl_testnet"),
        state=state,
        decision=decision,
        fill=None,
        indicators=indicators,
        equity=equity,
        state_before=state_before,
        state_after=state.state.value,
        config_hash=config_snapshot_hash(config),
        order_result=order_result,
        reconciliation_result=reconciliation_result,
        risk_status_override=risk_status_override,
    )


def _state_position_label(state) -> BotState:
    if state.current_position_qty <= EPSILON:
        return BotState.EXITED
    if state.addon_lots:
        return BotState.PYRAMID_LONG
    if state.runner_mode_active:
        return BotState.RUNNER_LONG
    if state.base_lot is not None:
        return BotState.BASE_LONG
    return state.state


def _is_recoverable_cleanup_halt(state) -> bool:
    reason = state.halt_reason or ""
    return state.state == BotState.HALTED and state.halted and any(
        reason.startswith(prefix) for prefix in RECOVERABLE_CLEANUP_HALT_PREFIXES
    )


def _clear_safe_cleanup_halt_if_aligned(state, exchange_qty: float, open_orders_count: int) -> bool:
    if not _is_recoverable_cleanup_halt(state):
        return False
    if state.pending_order is not None or open_orders_count != 0:
        return False
    if abs(state.current_position_qty - exchange_qty) > EPSILON:
        return False
    state.state = _state_position_label(state)
    state.halted = False
    state.halt_reason = None
    return True


def _fill_from_order_result(order_result: OrderResult) -> Fill | None:
    if order_result.filled_qty <= EPSILON:
        return None
    return Fill(
        action=ActionType.EXIT_ALL,
        qty=order_result.filled_qty,
        fill_price=order_result.avg_fill_px or order_result.limit_px or 0.0,
        slippage_bps=0.0,
        realized_pnl=order_result.realized_pnl,
        commission=order_result.commission,
        timestamp=order_result.created_at,
        order_id=order_result.oid,
        order_status=order_result.status.value,
    )


def _safe_to_cancel_target_pending(state, open_orders: list, coin: str) -> tuple[bool, int | None, str | None]:
    if state.pending_order is None:
        return False, None, "unexpected_open_order_without_local_pending"
    pending: PendingOrder = state.pending_order
    if not pending.reduce_only or pending.side.lower() != "sell":
        return False, None, "pending_order_not_reduce_only_sell"
    matching = [order for order in open_orders if order.oid == pending.oid and order.coin == coin]
    if len(matching) != 1:
        return False, None, "local_pending_order_not_open_on_exchange"
    if len(open_orders) != 1:
        return False, None, "multiple_open_orders_not_supported"
    return True, pending.oid, None


def _print_attempt_summary(attempt: int, state, snapshot, coin: str) -> None:
    print(f"attempt={attempt}")
    print("exchange_qty=" + str(_position_qty(snapshot, coin)))
    print("open_orders_count=" + str(snapshot.open_orders_count))
    print("state=" + state.state.value)
    print("halted=" + str(state.halted))
    print("halt_reason=" + str(state.halt_reason))
    print("local_qty=" + str(state.current_position_qty))
    print("pending_order=" + str(state.pending_order.oid if state.pending_order is not None else None))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervised testnet-only HL flatten loop")
    parser.add_argument("--config", required=True)
    parser.add_argument("--coin", required=True)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--aggressiveness-bps", type=float, default=10.0)
    parser.add_argument("--emergency-deviation-bps", type=float, default=25.0)
    args = parser.parse_args(argv)

    if not args.confirm:
        print("ERROR: --confirm is required")
        return 2
    if args.max_attempts <= 0:
        print("ERROR: --max-attempts must be > 0")
        return 2

    config = load_config(args.config)
    hl_cfg = config.hl or {}
    network = str(hl_cfg.get("network", "testnet"))
    coin = str(hl_cfg.get("coin", config.symbol.get("ticker", args.coin)))
    if network != "testnet" or str(config.bot.get("mode")) != "testnet":
        print("REFUSED: testnet_only")
        return 2
    if args.coin != coin:
        print(f"REFUSED: requested coin {args.coin} does not match config coin {coin}")
        return 2

    wallet = hl_cfg.get("wallet_address", "")
    private_key = get_private_key(config)
    client = HyperliquidClient(network=network)
    run_dir = Path(config.logging.get("run_dir", "reports/btc_hl_testnet"))
    state_path = run_dir / "state.json"
    state, terminal = load_state_or_halt(str(state_path), config.symbol.get("ticker", coin))
    if terminal:
        print("REFUSED: local_state_load_terminal")
        return 1

    print("network=" + network)
    print("account_address=" + _mask(wallet))
    print("mode=supervised_testnet_cleanup")

    for attempt in range(1, args.max_attempts + 1):
        snapshot = get_account_snapshot(
            wallet,
            client,
            include_spot=True,
            include_subaccounts=False,
            include_open_orders=True,
            collateral_mode=hl_cfg.get("collateral_mode", "unified"),
        )
        recent_fills = get_recent_user_fills(wallet, client)
        state, reconciliation = reconcile_state_with_exchange(state, snapshot, recent_fills, config)
        exchange_qty = _position_qty(snapshot, coin)
        mark_price = _mark_price(snapshot, coin)
        indicators = _synthetic_indicators(mark_price)
        equity = snapshot.effective_trading_collateral or snapshot.account_value or float(config.capital["starting_equity"])

        if snapshot.open_orders_count > 0:
            open_orders = [order for order in snapshot.open_orders if order.coin == coin]
            can_cancel, oid, blocker = _safe_to_cancel_target_pending(state, open_orders, coin)
            if not can_cancel or oid is None:
                print("REFUSED: " + str(blocker))
                _print_attempt_summary(attempt, state, snapshot, coin)
                write_state(state, str(state_path))
                return 1
            print("ACTION=cancel_exact_pending_order")
            print("oid=" + str(oid))
            if not cancel_order(coin, oid, private_key, client):
                state.halted = True
                state.state = BotState.HALTED
                state.halt_reason = "cleanup_loop_cancel_failed"
                write_state(state, str(state_path))
                _print_attempt_summary(attempt, state, snapshot, coin)
                return 1
            post_cancel = get_account_snapshot(
                wallet,
                client,
                include_spot=True,
                include_subaccounts=False,
                include_open_orders=True,
                collateral_mode=hl_cfg.get("collateral_mode", "unified"),
            )
            post_fills = get_recent_user_fills(wallet, client)
            state, reconciliation = reconcile_state_with_exchange(state, post_cancel, post_fills, config)
            if state.pending_order is not None and post_cancel.open_orders_count == 0:
                state.pending_order = None
            write_state(state, str(state_path))
            _print_attempt_summary(attempt, state, post_cancel, coin)
            continue

        if _clear_safe_cleanup_halt_if_aligned(state, exchange_qty, snapshot.open_orders_count):
            print("ACTION=clear_safe_cleanup_halt")
            write_state(state, str(state_path))

        if state.pending_order is not None:
            print("REFUSED: local_pending_order_without_exchange_open_order")
            _print_attempt_summary(attempt, state, snapshot, coin)
            write_state(state, str(state_path))
            return 1

        if exchange_qty <= EPSILON:
            state.current_position_qty = 0.0
            state.base_lot = None
            state.addon_lots = []
            state.state = BotState.EXITED
            state.halted = False
            state.halt_reason = None
            state.pending_order = None
            write_state(state, str(state_path))
            _print_attempt_summary(attempt, state, snapshot, coin)
            print("RESULT=FLAT")
            return 0

        if abs(state.current_position_qty - exchange_qty) > EPSILON:
            print("REFUSED: local_exchange_qty_mismatch")
            _print_attempt_summary(attempt, state, snapshot, coin)
            write_state(state, str(state_path))
            return 1

        if state.halted:
            print("REFUSED: non_recoverable_halt")
            _print_attempt_summary(attempt, state, snapshot, coin)
            write_state(state, str(state_path))
            return 1

        state_before = state.state.value
        decision = Decision(
            action=ActionType.EXIT_ALL,
            qty=exchange_qty,
            reason="supervised_testnet_cleanup_ioc",
            new_state=BotState.EXITED,
        )
        try:
            pricing_mark_price, best_bid, best_ask = _marketable_exit_reference_price(
                client,
                coin=coin,
                side="sell",
                cross_bps=args.aggressiveness_bps,
            )
        except Exception as exc:  # noqa: BLE001
            print("REFUSED: marketable_book_lookup_failed:" + str(exc))
            return 1

        execution_cfg = dict(config.execution)
        execution_cfg.update(
            {
                "time_in_force": "ioc",
                "exit_price_aggressiveness_bps": 0.0,
                "max_oracle_deviation_bps": float(args.emergency_deviation_bps),
            }
        )
        probe_config = config.model_copy(update={"execution": execution_cfg})
        order_result = execute_hl(
            decision,
            state,
            probe_config,
            client,
            wallet,
            private_key,
            mark_price=pricing_mark_price,
        )
        print("ACTION=submit_ioc_reduce_only")
        print("best_bid=" + str(best_bid))
        print("best_ask=" + str(best_ask))
        print("pricing_reference=" + str(pricing_mark_price))
        print("order_status=" + order_result.status.value)
        print("filled_qty=" + str(order_result.filled_qty))

        fill = _fill_from_order_result(order_result)
        if fill is not None:
            apply_fill(state, fill)
        state.pending_order = None

        if state.current_position_qty <= EPSILON:
            state.state = BotState.EXITED
            state.halted = False
            state.halt_reason = None
        elif order_result.status in {OrderResultStatus.PARTIALLY_FILLED, OrderResultStatus.FILLED}:
            state.state = _state_position_label(state)
            state.halted = False
            state.halt_reason = None
        elif order_result.status in {OrderResultStatus.REJECTED, OrderResultStatus.EXECUTION_ERROR, OrderResultStatus.UNKNOWN}:
            state.state = BotState.HALTED
            state.halted = True
            state.halt_reason = "cleanup_exit_failed:" + (
                order_result.exchange_error_sanitized
                or order_result.raw_status_sanitized
                or order_result.status.value
            )
        else:
            state.state = BotState.HALTED
            state.halted = True
            state.halt_reason = "ioc_flatten_unfilled_position_still_open"

        if state.halt_reason == "cleanup_exit_failed:None":
            state.halt_reason = "cleanup_exit_failed:" + (order_result.raw_status_sanitized or order_result.status.value)
        mark_to_market_state(state, mark_price)
        write_state(state, str(state_path))
        _log_cycle_safe(
            config=config,
            state=state,
            decision=decision,
            indicators=indicators,
            equity=equity,
            state_before=state_before,
            order_result=order_result,
            reconciliation_result=reconciliation,
            risk_status_override="halted" if state.halted else "ok",
        )

        post = get_account_snapshot(
            wallet,
            client,
            include_spot=True,
            include_subaccounts=False,
            include_open_orders=True,
            collateral_mode=hl_cfg.get("collateral_mode", "unified"),
        )
        _print_attempt_summary(attempt, state, post, coin)
        if _position_qty(post, coin) <= EPSILON and state.current_position_qty <= EPSILON:
            print("RESULT=FLAT")
            return 0

    print("RESULT=MAX_ATTEMPTS_REACHED")
    print("operator_next_step=inspect diagnostics; do not run strategy until position is flat or reviewed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
