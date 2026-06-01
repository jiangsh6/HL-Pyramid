"""One-cycle, reduce-only Hyperliquid position cleanup probe.

This script is intentionally narrow:
  - testnet only
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
from src.hl.auth import get_private_key
from src.hl.client import HyperliquidClient
from src.hl.precision import decimal_to_plain_string, format_hl_qty
from src.hl.pricing import compute_oracle_safe_exit_price
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


def main() -> int:
    parser = argparse.ArgumentParser(description="One-cycle reduce-only HL testnet flatten probe")
    parser.add_argument("--config", required=True)
    parser.add_argument("--coin", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--one-cycle", action="store_true")
    parser.add_argument(
        "--recover-from-cleanup-exit-halt",
        action="store_true",
        help="Allow retry only from a recoverable oracle-price cleanup exit HALT.",
    )
    args = parser.parse_args()

    if not args.confirm or not args.one_cycle:
        print("ERROR: --confirm and --one-cycle are required")
        return 2

    config = load_config(args.config)
    hl_cfg = config.hl or {}
    network = hl_cfg.get("network", "testnet")
    mode = config.bot.get("mode")
    coin = hl_cfg.get("coin", config.symbol.get("ticker", args.coin))
    if mode != "testnet" or network != "testnet":
        print("ERROR: refusing non-testnet config")
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

    print("network=testnet")
    print("mainnet_used=False")
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
    recover_from_halt = can_recover_cleanup_exit_halt(
        state=state,
        network=network,
        recover_flag=args.recover_from_cleanup_exit_halt,
        confirmed=args.confirm,
        open_orders_count=snapshot.open_orders_count,
    )
    if state.halted and not recover_from_halt:
        print("STOPPED_BEFORE_EXIT: local_state_halted")
        print("halt_reason=" + str(state.halt_reason))
        return 1

    position = next((p for p in snapshot.positions if p.coin == coin), None)
    exchange_qty = position.qty if position is not None else 0.0
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
        config.execution.get(
            "exit_price_aggressiveness_bps",
            hl_cfg.get("exit_price_aggressiveness_bps", 2),
        )
    )
    max_oracle_bps = float(
        config.execution.get(
            "max_oracle_deviation_bps",
            hl_cfg.get("max_oracle_deviation_bps", 5),
        )
    )
    price_info = compute_oracle_safe_exit_price(
        "sell",
        mark_price,
        None,
        sz_decimals,
        max_oracle_bps,
        exit_aggr_bps,
    )
    print("mark_oracle_proxy=" + str(price_info.oracle_price))
    print("raw_exit_px=" + str(price_info.raw_exit_px))
    print("formatted_exit_px=" + str(price_info.formatted_exit_px))
    print("max_deviation_bps=" + str(price_info.max_deviation_bps))

    order_result = execute_hl(
        decision,
        state,
        config,
        client,
        wallet,
        private_key,
        mark_price,
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
        state.pending_order = _pending_from_order_result(state, decision, order_result)
        state.state = BotState.HALTED
        state.halted = True
        state.halt_reason = "unfilled_emergency_exit"
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
            state.pending_order = _pending_from_order_result(state, decision, order_result)
            state.state = BotState.HALTED
            state.halted = True
            state.halt_reason = "residual_position_after_emergency_exit"
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
