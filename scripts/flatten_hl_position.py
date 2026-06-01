"""One-cycle, reduce-only Hyperliquid position cleanup probe.

This script is intentionally narrow:
  - testnet by default; mainnet only with explicit recovery gates
  - one coin only
  - reduce-only SELL only
  - no strategy entry/add logic
  - no recurring loop

It exists to validate the PR-004 fill-confirmed reduce/exit path against a
known testnet position. Secrets are loaded through the normal runtime path but
are never printed.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.config_loader import config_snapshot_hash, load_config
from src.core.models import (
    ActionType,
    BotState,
    Decision,
    EPSILON,
    IndicatorSnapshot,
    OrderResult,
    OrderResultStatus,
    PendingOrder,
)
from src.execution.hl_broker import execute_hl
from src.execution.reconciler import reconcile_state_with_exchange
from src.hl.account import get_account_snapshot, get_recent_user_fills
from src.hl.auth import get_private_key, validate_mainnet_intent
from src.hl.client import HyperliquidClient
from src.hl.precision import decimal_to_plain_string, format_hl_qty
from src.hl.pricing import compute_oracle_safe_exit_price
from src.notifications.telegram import TelegramNotifier, alert_toggle_enabled, notify_event
from src.reporting.logger import log_cycle
from src.reporting.state_writer import (
    apply_fill,
    load_state_or_halt,
    mark_to_market_state,
    update_dust_state,
    write_state,
)


def _mask(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr
    return addr[:6] + "..." + addr[-3:]



def _validate_recovery_network_intent(config, network: str, cli_mainnet: bool) -> bool:
    mode = str(config.bot.get("mode", ""))
    network = str(network)
    if mode == "mainnet" or network == "mainnet":
        if mode != "mainnet" or network != "mainnet":
            raise ValueError("mainnet recovery requires config mode and hl.network to both be mainnet")
        validate_mainnet_intent(config, cli_has_mainnet_flag=cli_mainnet)
        return True
    if cli_mainnet:
        raise ValueError("--mainnet was passed for a non-mainnet config")
    if mode != "testnet" or network != "testnet":
        raise ValueError("refusing non-testnet recovery config")
    return False


def _notify_exit_semantic(config, state_before: str, state, order_result, exchange_qty: float) -> None:
    if not alert_toggle_enabled(config.notifications or {}, "trade_alerts_enabled"):
        return
    notifier = TelegramNotifier.from_config(config.notifications or {})
    event_name = "position_closed" if state.current_position_qty <= EPSILON else "partial_fill_detected"
    event = {
        "type": "semantic",
        "event": event_name,
        "run_id": str(config.bot.get("run_id") or "-"),
        "network": str((config.hl or {}).get("network", config.bot.get("mode", "-"))),
        "state_before": state_before,
        "state_after": state.state.value,
        "order_id": order_result.oid,
        "qty": order_result.submitted_qty,
        "fill_qty": order_result.filled_qty,
        "price": order_result.avg_fill_px or order_result.limit_px,
        "exchange_position_qty": exchange_qty,
        "local_position_qty": state.current_position_qty,
    }
    try:
        notify_event(notifier, event)
    except Exception:  # noqa: BLE001
        return


def _synthetic_indicators(mark_price: float) -> IndicatorSnapshot:
    today = date.today()
    return IndicatorSnapshot(
        date=today,
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


def _pending_from_order_result(state, decision: Decision, order_result) -> PendingOrder:
    return PendingOrder(
        oid=order_result.oid,
        client_order_id=order_result.client_order_id,
        symbol=state.symbol,
        action=decision.action.value,
        side=order_result.side,
        reduce_only=order_result.reduce_only,
        order_type="limit",
        qty=order_result.submitted_qty,
        qty_submitted=order_result.submitted_qty,
        qty_filled=order_result.filled_qty,
        qty_remaining=order_result.remaining_qty,
        limit_px=order_result.limit_px or order_result.avg_fill_px or 0.0,
        status=order_result.status.value,
        created_at=order_result.created_at,
        last_checked_at=order_result.created_at,
        state_before=order_result.state_before,
        intended_state_after=order_result.intended_state_after,
        applied_state_after=order_result.applied_state_after,
    )


def _fill_from_order_result(order_result):
    from src.core.models import Fill

    if order_result.filled_qty <= EPSILON:
        return None
    return Fill(
        action=order_result.action,
        qty=order_result.filled_qty,
        fill_price=order_result.avg_fill_px or order_result.limit_px or 0.0,
        slippage_bps=0.0,
        realized_pnl=order_result.realized_pnl,
        commission=order_result.commission,
        timestamp=order_result.created_at,
        order_id=order_result.oid,
        order_status=order_result.status.value,
    )


def _log_cleanup_cycle(
    *,
    config,
    state,
    decision,
    fill,
    indicators,
    equity,
    state_before,
    order_result=None,
    reconciliation_result=None,
    risk_status_override=None,
) -> None:
    run_dir = config.logging.get("run_dir", "reports/btc_hl_testnet")
    log_cycle(
        run_dir=run_dir,
        state=state,
        decision=decision,
        fill=fill,
        indicators=indicators,
        equity=equity,
        state_before=state_before,
        state_after=state.state.value,
        config_hash=config_snapshot_hash(config),
        order_result=order_result,
        reconciliation_result=reconciliation_result,
        risk_status_override=risk_status_override,
    )


RECOVERABLE_CLEANUP_EXIT_HALT_PREFIX = "cleanup_exit_failed:Price too far from oracle"
RECOVERABLE_IOC_NO_MATCH_EXIT_HALT_PREFIX = (
    "cleanup_exit_failed:Order could not immediately match against any resting orders"
)
RECOVERABLE_IOC_FLATTEN_HALT_REASON = "ioc_flatten_unfilled_position_still_open"
TARGET_RUNNER_VALIDATION_HALT_REASON = "exit_order_canceled_position_still_open"


def can_recover_cleanup_exit_halt(
    *,
    state,
    network: str,
    recover_flag: bool,
    confirmed: bool,
    open_orders_count: int,
) -> bool:
    return (
        network == "testnet"
        and recover_flag
        and confirmed
        and state.state == BotState.HALTED
        and state.halted
        and (state.halt_reason or "").startswith(RECOVERABLE_CLEANUP_EXIT_HALT_PREFIX)
        and state.pending_order is None
        and open_orders_count == 0
    )


def can_recover_target_runner_validation_halt(
    *,
    state,
    network: str,
    recover_flag: bool,
    confirmed: bool,
    open_orders_count: int,
    exchange_qty: float,
) -> bool:
    return (
        network == "testnet"
        and recover_flag
        and confirmed
        and state.state == BotState.HALTED
        and state.halted
        and state.halt_reason == TARGET_RUNNER_VALIDATION_HALT_REASON
        and state.pending_order is None
        and open_orders_count == 0
        and exchange_qty > EPSILON
        and state.current_position_qty > EPSILON
        and abs(state.current_position_qty - exchange_qty) <= EPSILON
    )


def can_recover_ioc_flatten_halt(
    *,
    state,
    network: str,
    recover_flag: bool,
    confirmed: bool,
    open_orders_count: int,
    exchange_qty: float,
) -> bool:
    halt_reason = state.halt_reason or ""
    return (
        network == "testnet"
        and recover_flag
        and confirmed
        and state.state == BotState.HALTED
        and state.halted
        and (
            halt_reason == RECOVERABLE_IOC_FLATTEN_HALT_REASON
            or halt_reason.startswith(RECOVERABLE_IOC_NO_MATCH_EXIT_HALT_PREFIX)
        )
        and state.pending_order is None
        and open_orders_count == 0
        and exchange_qty > EPSILON
        and state.current_position_qty > EPSILON
        and abs(state.current_position_qty - exchange_qty) <= EPSILON
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="One-cycle reduce-only HL recovery flatten probe")
    parser.add_argument("--config", required=True)
    parser.add_argument("--coin", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--one-cycle", action="store_true")
    parser.add_argument(
        "--recover-from-cleanup-exit-halt",
        action="store_true",
        help="Allow retry only from a recoverable oracle-price cleanup exit HALT.",
    )
    parser.add_argument(
        "--recover-from-target-runner-validation-halt",
        action="store_true",
        help="Allow IOC flatten only from the Bundle 2 target-runner validation HALT.",
    )
    parser.add_argument(
        "--recover-from-ioc-flatten-halt",
        action="store_true",
        help="Allow retry only from an IOC flatten no-match/unfilled HALT.",
    )
    parser.add_argument(
        "--force-ioc-no-match-validation",
        action="store_true",
        help="Probe-only: intentionally price IOC sell above market to validate no-match handling.",
    )
    parser.add_argument("--time-in-force", choices=("gtc", "ioc"), default=None)
    parser.add_argument("--aggressiveness-bps", type=float, default=None)
    parser.add_argument("--exit-aggressiveness-bps", type=float, default=None)
    parser.add_argument("--max-oracle-deviation-bps", type=float, default=None)
    parser.add_argument("--mainnet", action="store_true")
    args = parser.parse_args()

    if not args.confirm or not args.one_cycle:
        print("ERROR: --confirm and --one-cycle are required")
        return 2

    config = load_config(args.config)
    hl_cfg = config.hl or {}
    network = hl_cfg.get("network", "testnet")
    coin = hl_cfg.get("coin", config.symbol.get("ticker", args.coin))
    try:
        mainnet_used = _validate_recovery_network_intent(config, network, args.mainnet)
    except ValueError as exc:
        print("ERROR: " + str(exc))
        return 2
    if args.coin != coin:
        print(f"ERROR: requested coin {args.coin} does not match config coin {coin}")
        return 2

    wallet = hl_cfg.get("wallet_address", "")
    private_key = get_private_key(config)
    client = HyperliquidClient(network=network)
    run_dir = Path(config.logging.get("run_dir", "reports/btc_hl_testnet"))
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"

    print("network=" + str(network))
    print("mainnet_used=" + str(mainnet_used))
    print("account_address=" + _mask(wallet))
    print("private_key_loaded=True")

    state, terminal = load_state_or_halt(str(state_path), config.symbol.get("ticker", coin))
    if terminal:
        print("STOPPED_BEFORE_EXIT: local_state_load_terminal")
        return 1

    snapshot = get_account_snapshot(
        wallet,
        client,
        include_spot=True,
        include_subaccounts=True,
        include_open_orders=True,
        collateral_mode=hl_cfg.get("collateral_mode", "unified"),
    )
    if snapshot.open_orders_count > 0:
        print(f"STOPPED_BEFORE_EXIT: open_orders_count={snapshot.open_orders_count}")
        return 1
    position = next((p for p in snapshot.positions if p.coin == coin), None)
    exchange_qty = position.qty if position is not None else 0.0

    recover_from_cleanup_halt = can_recover_cleanup_exit_halt(
        state=state,
        network=network,
        recover_flag=args.recover_from_cleanup_exit_halt,
        confirmed=args.confirm,
        open_orders_count=snapshot.open_orders_count,
    )
    recover_from_target_runner_halt = can_recover_target_runner_validation_halt(
        state=state,
        network=network,
        recover_flag=args.recover_from_target_runner_validation_halt,
        confirmed=args.confirm,
        open_orders_count=snapshot.open_orders_count,
        exchange_qty=exchange_qty,
    )
    recover_from_ioc_flatten_halt = can_recover_ioc_flatten_halt(
        state=state,
        network=network,
        recover_flag=args.recover_from_ioc_flatten_halt,
        confirmed=args.confirm,
        open_orders_count=snapshot.open_orders_count,
        exchange_qty=exchange_qty,
    )
    recover_from_halt = recover_from_cleanup_halt or recover_from_target_runner_halt or recover_from_ioc_flatten_halt
    if state.halted and not recover_from_halt:
        print("STOPPED_BEFORE_EXIT: local_state_halted")
        print("halt_reason=" + str(state.halt_reason))
        return 1

    if exchange_qty <= EPSILON:
        print("STOPPED_BEFORE_EXIT: no_position_to_exit")
        return 1
    if exchange_qty < -EPSILON:
        print("STOPPED_BEFORE_EXIT: unexpected_short_position")
        return 1

    recent_fills = get_recent_user_fills(wallet, client)
    state, reconciliation = reconcile_state_with_exchange(state, snapshot, recent_fills, config)
    mark_price = position.mark_price or position.entry_price
    indicators = _synthetic_indicators(mark_price)
    equity = snapshot.effective_trading_collateral or snapshot.account_value or float(config.capital["starting_equity"])

    if recover_from_halt and reconciliation.status.value != "halted":
        if abs(state.current_position_qty - exchange_qty) > EPSILON or state.pending_order is not None:
            print("STOPPED_BEFORE_EXIT: recovery_state_not_aligned")
            return 1
        state.state = BotState.BASE_LONG if not state.addon_lots else BotState.PYRAMID_LONG
        if recover_from_target_runner_halt and not state.addon_lots:
            state.state = BotState.STARTER_LONG
        state.halted = False
        state.halt_reason = None
        print("recovery_guard=allowed")
    elif reconciliation.status.value == "halted" or state.halted:
        write_state(state, str(state_path))
        decision = Decision(action=ActionType.NO_ACTION, reason=reconciliation.halt_reason or "reconciliation_halted")
        _log_cleanup_cycle(
            config=config,
            state=state,
            decision=decision,
            fill=None,
            indicators=indicators,
            equity=equity,
            state_before=state.state.value,
            reconciliation_result=reconciliation,
            risk_status_override="halted",
        )
        print("STOPPED_BEFORE_EXIT: reconciliation_halted")
        print("halt_reason=" + str(state.halt_reason))
        return 1

    if mainnet_used and abs(state.current_position_qty - exchange_qty) > EPSILON:
        print("STOPPED_BEFORE_EXIT: mainnet_position_mismatch")
        return 1

    sz_decimals = int(hl_cfg.get("sz_decimals", state.sz_decimals or 0) or 0)
    qty_dec = format_hl_qty(exchange_qty, sz_decimals, rounding="down")
    qty = float(qty_dec)
    min_size = float(hl_cfg.get("min_size", 0.0) or 0.0)

    mark_to_market_state(state, mark_price)
    if exchange_qty < min_size and update_dust_state(state, mark_price, config):
        state_before = state.state.value
        decision = Decision(
            action=ActionType.EXIT_ALL,
            qty=exchange_qty,
            reason="qty_below_min_size_dust_position",
            blockers=["dust_position_below_min_size"],
            new_state=BotState.HALTED,
        )
        order_result = OrderResult(
            status=OrderResultStatus.BLOCKED_PRE_ORDER,
            action=ActionType.EXIT_ALL,
            side="sell",
            reduce_only=True,
            submitted_qty=exchange_qty,
            filled_qty=0.0,
            remaining_qty=exchange_qty,
            raw_status_sanitized="qty_below_min_size_dust_position",
            exchange_error_sanitized="qty_below_min_size_dust_position",
            state_before=state_before,
            intended_state_after=BotState.HALTED.value,
            applied_state_after=state.state.value,
            created_at=datetime.now(timezone.utc),
        )
        write_state(state, str(state_path))
        _log_cleanup_cycle(
            config=config,
            state=state,
            decision=decision,
            fill=None,
            indicators=indicators,
            equity=equity,
            state_before=state_before,
            order_result=order_result,
            reconciliation_result=reconciliation,
            risk_status_override="dust_position",
        )
        print(f"STOPPED_BEFORE_EXIT: qty_below_min_size_dust_position qty={exchange_qty}")
        print("dust_position=True")
        print("dust_qty=" + str(state.dust_qty))
        print("dust_notional=" + str(state.dust_notional))
        return 1
    if qty < min_size or qty <= EPSILON:
        print(f"STOPPED_BEFORE_EXIT: qty_below_min_size qty={decimal_to_plain_string(qty_dec)}")
        return 1
    state_before = state.state.value
    decision = Decision(
        action=ActionType.EXIT_ALL,
        qty=qty,
        reason="manual_reduce_only_cleanup_exit",
        new_state=BotState.EXITED,
    )
    print("pre_exit_state=" + state_before)
    print(f"exchange_qty={exchange_qty}")
    print("exit_side=sell")
    print("reduce_only=True")
    print("exit_qty=" + decimal_to_plain_string(qty_dec))
    exit_aggr_bps = float(
        args.aggressiveness_bps
        if args.aggressiveness_bps is not None
        else args.exit_aggressiveness_bps
        if args.exit_aggressiveness_bps is not None
        else config.execution.get(
            "exit_price_aggressiveness_bps",
            hl_cfg.get("exit_price_aggressiveness_bps", 2),
        )
    )
    max_oracle_bps = float(
        args.max_oracle_deviation_bps
        if args.max_oracle_deviation_bps is not None
        else config.execution.get(
            "max_oracle_deviation_bps",
            hl_cfg.get("max_oracle_deviation_bps", 5),
        )
    )
    time_in_force = args.time_in_force or str(config.execution.get("time_in_force", "gtc"))
    if args.force_ioc_no_match_validation and time_in_force.lower() != "ioc":
        print("STOPPED_BEFORE_EXIT: no_match_validation_requires_ioc")
        return 1
    pricing_mark_price = mark_price * 1.05 if args.force_ioc_no_match_validation else mark_price
    price_info = compute_oracle_safe_exit_price(
        "sell",
        pricing_mark_price,
        None,
        sz_decimals,
        max_oracle_bps,
        exit_aggr_bps,
    )
    print("mark_oracle_proxy=" + str(price_info.oracle_price))
    print("raw_exit_px=" + str(price_info.raw_exit_px))
    print("formatted_exit_px=" + str(price_info.formatted_exit_px))
    print("max_deviation_bps=" + str(price_info.max_deviation_bps))
    print("exit_aggressiveness_bps=" + str(exit_aggr_bps))
    print("time_in_force=" + time_in_force)
    print("force_ioc_no_match_validation=" + str(args.force_ioc_no_match_validation))

    execution_cfg = dict(config.execution)
    execution_cfg.update(
        {
            "time_in_force": time_in_force,
            "exit_price_aggressiveness_bps": exit_aggr_bps,
            "max_oracle_deviation_bps": max_oracle_bps,
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
        pricing_mark_price,
    )
    fill = _fill_from_order_result(order_result)
    risk_status = None

    if order_result.status in {
        OrderResultStatus.EXECUTION_ERROR,
        OrderResultStatus.REJECTED,
        OrderResultStatus.UNKNOWN,
        OrderResultStatus.CANCEL_FAILED,
    }:
        state.state = BotState.HALTED
        state.halted = True
        state.halt_reason = (
            "cleanup_exit_failed:"
            + (order_result.exchange_error_sanitized or order_result.raw_status_sanitized or order_result.status.value)
        )[:180]
        order_result.applied_state_after = state.state.value
        risk_status = "halted"
    elif order_result.status == OrderResultStatus.SUBMITTED_UNFILLED:
        is_ioc = time_in_force.lower() == "ioc"
        state.pending_order = None if is_ioc else _pending_from_order_result(state, decision, order_result)
        state.state = BotState.HALTED
        state.halted = True
        state.halt_reason = "ioc_flatten_unfilled_position_still_open" if is_ioc else "unfilled_emergency_exit"
        order_result.applied_state_after = state.state.value
        risk_status = "halted"
    elif fill is not None:
        apply_fill(state, fill)
        if state.current_position_qty <= EPSILON:
            state.state = BotState.EXITED
            state.halted = False
            state.halt_reason = None
            state.pending_order = None
            order_result.applied_state_after = state.state.value
            risk_status = "ok"
        else:
            is_ioc = time_in_force.lower() == "ioc"
            state.pending_order = None if is_ioc else _pending_from_order_result(state, decision, order_result)
            state.state = BotState.HALTED
            state.halted = True
            state.halt_reason = (
                "residual_position_after_ioc_emergency_exit"
                if is_ioc
                else "residual_position_after_emergency_exit"
            )
            order_result.applied_state_after = state.state.value
            risk_status = "halted"

    mark_to_market_state(state, mark_price)
    write_state(state, str(state_path))
    _log_cleanup_cycle(
        config=config,
        state=state,
        decision=decision,
        fill=fill,
        indicators=indicators,
        equity=equity,
        state_before=state_before,
        order_result=order_result,
        reconciliation_result=reconciliation,
        risk_status_override=risk_status,
    )

    post = get_account_snapshot(
        wallet,
        client,
        include_spot=True,
        include_subaccounts=False,
        include_open_orders=True,
        collateral_mode=hl_cfg.get("collateral_mode", "unified"),
    )
    post_position = next((p for p in post.positions if p.coin == coin), None)
    post_qty = post_position.qty if post_position is not None else 0.0

    if fill is not None and order_result.filled_qty > EPSILON:
        _notify_exit_semantic(config, state_before, state, order_result, post_qty)

    print("order_status=" + order_result.status.value)
    print("oid=" + str(order_result.oid))
    print("limit_px=" + str(order_result.limit_px))
    print("filled_qty=" + str(order_result.filled_qty))
    print("remaining_qty=" + str(order_result.remaining_qty))
    print("avg_fill_px=" + str(order_result.avg_fill_px))
    print("post_exchange_qty=" + str(post_qty))
    print("post_open_orders_count=" + str(post.open_orders_count))
    print("final_state=" + state.state.value)
    print("halted=" + str(state.halted))
    print("halt_reason=" + str(state.halt_reason))
    return 0 if not state.halted else 1


if __name__ == "__main__":
    raise SystemExit(main())
