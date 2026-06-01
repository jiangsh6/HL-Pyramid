"""One-cycle, explicit Hyperliquid reduce-only dust close probe.

This is not a normal strategy path. It exists only to test whether HL accepts
an exact reduce-only close below configured hl.min_size.
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
    return IndicatorSnapshot(
        date=date.today(),
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


def can_attempt_dust_close(*, state, network: str, confirmed: bool, open_orders_count: int, min_size: float) -> bool:
    return (
        network == "testnet"
        and confirmed
        and state.state == BotState.HALTED
        and state.halted
        and state.halt_reason == "dust_position_below_min_size"
        and state.dust_position
        and state.pending_order is None
        and open_orders_count == 0
        and state.current_position_qty > EPSILON
        and state.current_position_qty < min_size
    )


def _pending_from_order_result(state, decision: Decision, order_result) -> PendingOrder:
    return PendingOrder(
        oid=order_result.oid,
        client_order_id=order_result.client_order_id,
        symbol=state.symbol,
        action="exit_dust",
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


def _log_cycle(
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
    log_cycle(
        run_dir=config.logging.get("run_dir", "reports/btc_hl_testnet"),
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


def main() -> int:
    parser = argparse.ArgumentParser(description="One-cycle explicit HL testnet dust close probe")
    parser.add_argument("--config", required=True)
    parser.add_argument("--coin", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--one-cycle", action="store_true")
    args = parser.parse_args()

    if not args.confirm or not args.one_cycle:
        print("ERROR: --confirm and --one-cycle are required")
        return 2

    config = load_config(args.config)
    hl_cfg = config.hl or {}
    policy = hl_cfg.get("dust_policy") or {}
    network = hl_cfg.get("network", "testnet")
    mode = config.bot.get("mode")
    coin = hl_cfg.get("coin", config.symbol.get("ticker", args.coin))
    if mode != "testnet" or network != "testnet":
        print("ERROR: refusing non-testnet config")
        return 2
    if args.coin != coin:
        print(f"ERROR: requested coin {args.coin} does not match config coin {coin}")
        return 2
    if not policy.get("allow_reduce_only_dust_close", False):
        print("STOPPED_BEFORE_DUST_CLOSE: allow_reduce_only_dust_close_false")
        return 1
    if policy.get("allow_round_up_to_min_size_for_reduce_only", False):
        print("ERROR: refusing dust close with round-up enabled")
        return 2

    wallet = hl_cfg.get("wallet_address", "")
    private_key = get_private_key(config)
    client = HyperliquidClient(network=network)
    run_dir = Path(config.logging.get("run_dir", "reports/btc_hl_testnet"))
    state_path = run_dir / "state.json"

    print("network=testnet")
    print("mainnet_used=False")
    print("account_address=" + _mask(wallet))
    print("private_key_loaded=True")

    state, terminal = load_state_or_halt(str(state_path), config.symbol.get("ticker", coin))
    if terminal:
        print("STOPPED_BEFORE_DUST_CLOSE: local_state_load_terminal")
        return 1

    snapshot = get_account_snapshot(
        wallet,
        client,
        include_spot=True,
        include_subaccounts=True,
        include_open_orders=True,
        collateral_mode=hl_cfg.get("collateral_mode", "unified"),
    )
    min_size = float(hl_cfg.get("min_size", 0.0) or 0.0)
    if not can_attempt_dust_close(
        state=state,
        network=network,
        confirmed=args.confirm,
        open_orders_count=snapshot.open_orders_count,
        min_size=min_size,
    ):
        print("STOPPED_BEFORE_DUST_CLOSE: guard_failed")
        print("state=" + state.state.value)
        print("halt_reason=" + str(state.halt_reason))
        print("dust_position=" + str(state.dust_position))
        print("open_orders_count=" + str(snapshot.open_orders_count))
        return 1

    position = next((p for p in snapshot.positions if p.coin == coin), None)
    exchange_qty = position.qty if position is not None else 0.0
    if exchange_qty <= EPSILON:
        print("STOPPED_BEFORE_DUST_CLOSE: no_exchange_position")
        return 1
    if abs(exchange_qty - state.current_position_qty) > EPSILON:
        print("STOPPED_BEFORE_DUST_CLOSE: local_exchange_qty_mismatch")
        print("exchange_qty=" + str(exchange_qty))
        print("local_qty=" + str(state.current_position_qty))
        return 1

    recent_fills = get_recent_user_fills(wallet, client)
    state, reconciliation = reconcile_state_with_exchange(state, snapshot, recent_fills, config)
    if state.pending_order is not None or reconciliation.open_orders_count > 0:
        print("STOPPED_BEFORE_DUST_CLOSE: pending_or_open_order_after_reconciliation")
        return 1
    if abs(state.current_position_qty - exchange_qty) > EPSILON:
        print("STOPPED_BEFORE_DUST_CLOSE: reconciliation_qty_mismatch")
        return 1

    mark_price = position.mark_price or position.entry_price
    indicators = _synthetic_indicators(mark_price)
    equity = snapshot.effective_trading_collateral or snapshot.account_value or float(config.capital["starting_equity"])
    mark_to_market_state(state, mark_price)
    update_dust_state(state, mark_price, config)

    sz_decimals = int(hl_cfg.get("sz_decimals", state.sz_decimals or 0) or 0)
    qty_dec = format_hl_qty(exchange_qty, sz_decimals, rounding="down")
    qty = float(qty_dec)
    if qty <= EPSILON or qty > exchange_qty + EPSILON:
        print("STOPPED_BEFORE_DUST_CLOSE: invalid_formatted_qty")
        return 1

    # This explicit probe bypasses the bot's normal min_size pre-order gate so
    # the exchange can tell us whether exact reduce-only dust closes are allowed.
    probe_hl_cfg = dict(hl_cfg)
    probe_hl_cfg["min_size"] = 0.0
    probe_config = config.model_copy(update={"hl": probe_hl_cfg})

    exit_aggr_bps = float(config.execution.get("exit_price_aggressiveness_bps", 2))
    max_oracle_bps = float(config.execution.get("max_oracle_deviation_bps", 5))
    price_info = compute_oracle_safe_exit_price(
        "sell",
        mark_price,
        None,
        sz_decimals,
        max_oracle_bps,
        exit_aggr_bps,
    )
    state_before = state.state.value
    decision = Decision(
        action=ActionType.EXIT_ALL,
        qty=qty,
        reason="explicit_reduce_only_dust_close_probe",
        new_state=BotState.EXITED,
    )
    print("pre_dust_state=" + state_before)
    print("exchange_qty=" + str(exchange_qty))
    print("dust_close_side=sell")
    print("reduce_only=True")
    print("dust_close_qty=" + decimal_to_plain_string(qty_dec))
    print("raw_exit_px=" + str(price_info.raw_exit_px))
    print("formatted_exit_px=" + str(price_info.formatted_exit_px))
    print("max_deviation_bps=" + str(price_info.max_deviation_bps))

    order_result = execute_hl(
        decision,
        state,
        probe_config,
        client,
        wallet,
        private_key,
        mark_price,
    )
    fill = _fill_from_order_result(order_result)
    risk_status = "dust_position"

    if order_result.status in {
        OrderResultStatus.EXECUTION_ERROR,
        OrderResultStatus.REJECTED,
        OrderResultStatus.UNKNOWN,
        OrderResultStatus.CANCEL_FAILED,
    }:
        state.state = BotState.HALTED
        state.halted = True
        state.halt_reason = (
            "dust_close_rejected:"
            + (order_result.exchange_error_sanitized or order_result.raw_status_sanitized or order_result.status.value)
        )[:180]
        order_result.applied_state_after = state.state.value
        update_dust_state(state, mark_price, config)
        risk_status = "dust_position"
    elif order_result.status == OrderResultStatus.SUBMITTED_UNFILLED:
        state.pending_order = _pending_from_order_result(state, decision, order_result)
        state.state = BotState.HALTED
        state.halted = True
        state.halt_reason = "dust_close_submitted_unfilled"
        order_result.applied_state_after = state.state.value
        risk_status = "dust_position"
    elif fill is not None:
        apply_fill(state, fill)
        if state.current_position_qty <= EPSILON:
            state.state = BotState.EXITED
            state.halted = False
            state.halt_reason = None
            state.pending_order = None
            state.dust_position = False
            state.dust_qty = 0.0
            state.dust_notional = 0.0
            state.dust_reason = None
            risk_status = "ok"
        else:
            state.state = BotState.HALTED
            state.halted = True
            state.halt_reason = "dust_close_partial"
            update_dust_state(state, mark_price, config)
            if order_result.remaining_qty > EPSILON:
                state.pending_order = _pending_from_order_result(state, decision, order_result)
            risk_status = "dust_position"
        order_result.applied_state_after = state.state.value

    mark_to_market_state(state, mark_price)
    write_state(state, str(state_path))
    _log_cycle(
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
    print("sanitized_rejection=" + str(order_result.exchange_error_sanitized))
    print("post_exchange_qty=" + str(post_qty))
    print("post_open_orders_count=" + str(post.open_orders_count))
    print("final_state=" + state.state.value)
    print("halted=" + str(state.halted))
    print("halt_reason=" + str(state.halt_reason))
    print("dust_position=" + str(state.dust_position))
    print("dust_qty=" + str(state.dust_qty))
    print("pending_order=" + str(state.pending_order.oid if state.pending_order is not None else None))
    return 0 if order_result.status in {OrderResultStatus.FILLED, OrderResultStatus.SUBMITTED_UNFILLED} else 1


if __name__ == "__main__":
    raise SystemExit(main())
