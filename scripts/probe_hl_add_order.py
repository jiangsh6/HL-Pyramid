"""One-cycle, controlled Hyperliquid add-on order probe.

This script bypasses strategy signals but not execution lifecycle safety:
  - testnet only
  - one BUY add-on only
  - no recurring loop
  - no duplicate order if local/exchange mismatch or pending order exists
  - state mutation only through OrderResult filled_qty and apply_fill()
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
from src.execution.probe_sizing import (
    get_probe_addon_qty,
    get_probe_qty_multiplier,
    validate_probe_qty_above_min,
)
from src.execution.reconciler import reconcile_state_with_exchange
from src.hl.account import get_account_snapshot, get_recent_user_fills
from src.hl.auth import get_private_key
from src.hl.client import HyperliquidClient
from src.hl.precision import decimal_to_plain_string, format_hl_qty
from src.reporting.logger import log_cycle
from src.reporting.state_writer import (
    apply_fill,
    load_state_or_halt,
    mark_to_market_state,
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


def _log_probe_cycle(
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
    parser = argparse.ArgumentParser(description="One-cycle controlled HL testnet add-on probe")
    parser.add_argument("--config", required=True)
    parser.add_argument("--coin", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--one-cycle", action="store_true")
    parser.add_argument("--qty", type=float, default=None)
    parser.add_argument("--allow-min-size-edge-test", action="store_true")
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
        print("STOPPED_BEFORE_ADD: local_state_load_terminal")
        return 1
    if state.pending_order is not None:
        print("STOPPED_BEFORE_ADD: local_pending_order_exists")
        return 1
    if state.state not in {BotState.STARTER_LONG, BotState.BASE_LONG, BotState.RUNNER_LONG, BotState.PYRAMID_LONG}:
        print("STOPPED_BEFORE_ADD: local_state_not_long")
        print("state=" + state.state.value)
        return 1
    if state.base_lot is None or state.current_position_qty <= EPSILON:
        print("STOPPED_BEFORE_ADD: local_position_missing")
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
        print(f"STOPPED_BEFORE_ADD: open_orders_count={snapshot.open_orders_count}")
        return 1

    position = next((p for p in snapshot.positions if p.coin == coin), None)
    exchange_qty = position.qty if position is not None else 0.0
    if exchange_qty <= EPSILON:
        print("STOPPED_BEFORE_ADD: no_exchange_position")
        return 1
    if abs(exchange_qty - state.current_position_qty) > EPSILON:
        print("STOPPED_BEFORE_ADD: local_exchange_qty_mismatch")
        print("exchange_qty=" + str(exchange_qty))
        print("local_qty=" + str(state.current_position_qty))
        return 1

    recent_fills = get_recent_user_fills(wallet, client)
    state, reconciliation = reconcile_state_with_exchange(state, snapshot, recent_fills, config)
    if reconciliation.status.value == "halted" or state.halted:
        write_state(state, str(state_path))
        print("STOPPED_BEFORE_ADD: reconciliation_halted")
        print("halt_reason=" + str(state.halt_reason))
        return 1
    if state.pending_order is not None:
        print("STOPPED_BEFORE_ADD: pending_after_reconciliation")
        return 1

    mark_price = position.mark_price or position.entry_price
    indicators = _synthetic_indicators(mark_price)
    equity = snapshot.effective_trading_collateral or snapshot.account_value or float(config.capital["starting_equity"])
    mark_to_market_state(state, mark_price)

    sz_decimals = int(hl_cfg.get("sz_decimals", state.sz_decimals or 0) or 0)
    raw_qty = get_probe_addon_qty(config, mark_price, explicit_qty=args.qty)
    qty_dec = format_hl_qty(raw_qty, sz_decimals, rounding="down")
    qty = float(qty_dec)
    min_size = float(hl_cfg.get("min_size", 0.0) or 0.0)
    blocker = validate_probe_qty_above_min(
        qty,
        min_size,
        get_probe_qty_multiplier(config),
        allow_min_size_edge_test=args.allow_min_size_edge_test,
    )
    if blocker is not None:
        print(f"STOPPED_BEFORE_ADD: {blocker} qty={decimal_to_plain_string(qty_dec)}")
        return 1

    state_before = state.state.value
    decision = Decision(
        action=ActionType.BUY_ADDON,
        qty=qty,
        reason="manual_controlled_addon_probe",
        new_state=BotState.PYRAMID_LONG,
    )
    print("pre_add_state=" + state_before)
    print(f"exchange_qty={exchange_qty}")
    print("add_side=buy")
    print("reduce_only=False")
    print("raw_add_qty=" + str(raw_qty))
    print("add_qty=" + decimal_to_plain_string(qty_dec))

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
            "addon_order_failed:"
            + (order_result.exchange_error_sanitized or order_result.raw_status_sanitized or order_result.status.value)
        )[:180]
        order_result.applied_state_after = state.state.value
        risk_status = "halted"
    elif order_result.status == OrderResultStatus.SUBMITTED_UNFILLED:
        state.pending_order = _pending_from_order_result(state, decision, order_result)
        order_result.applied_state_after = state.state.value
        risk_status = "pending_order_open"
    elif fill is not None:
        apply_fill(state, fill)
        state.state = BotState.PYRAMID_LONG
        state.halted = False
        state.halt_reason = None
        order_result.applied_state_after = state.state.value
        if order_result.remaining_qty > EPSILON:
            state.pending_order = _pending_from_order_result(state, decision, order_result)
            risk_status = "pending_order_open"
        else:
            state.pending_order = None
            risk_status = "ok"

    mark_to_market_state(state, mark_price)
    write_state(state, str(state_path))
    _log_probe_cycle(
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
    print("submitted_qty=" + str(order_result.submitted_qty))
    print("filled_qty=" + str(order_result.filled_qty))
    print("remaining_qty=" + str(order_result.remaining_qty))
    print("limit_px=" + str(order_result.limit_px))
    print("avg_fill_px=" + str(order_result.avg_fill_px))
    print("post_exchange_qty=" + str(post_qty))
    print("post_open_orders_count=" + str(post.open_orders_count))
    print("final_state=" + state.state.value)
    print("addon_lots=" + str([lot.qty for lot in state.addon_lots]))
    print("add_count=" + str(state.add_count))
    print("last_add_price=" + str(state.last_add_price))
    print("halted=" + str(state.halted))
    print("halt_reason=" + str(state.halt_reason))
    return 0 if not state.halted else 1


if __name__ == "__main__":
    raise SystemExit(main())
