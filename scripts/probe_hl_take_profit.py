"""One-cycle, controlled Hyperliquid layered take-profit probe.

This script bypasses strategy signals but not lifecycle safety:
  - testnet only
  - one reduce-only SELL take-profit order only
  - no recurring loop
  - no opening/add/flatten order
  - TP level is marked triggered only after confirmed filled_qty
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


def _pending_from_order_result(state, decision: Decision, order_result, *, tp_level: int) -> PendingOrder:
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
        source_decision_id=f"tp_level:{tp_level}",
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


def _tp_level_qty(config, state, level: int, min_size: float) -> float:
    levels = config.take_profit.get("layered_profit", {}).get("levels", [])
    reduce_pct = 0.0
    if 0 <= level < len(levels):
        reduce_pct = float(levels[level].get("reduce_pct_of_total_position", 0.0) or 0.0)
    requested = state.current_position_qty * reduce_pct
    if requested <= EPSILON:
        requested = min_size
    return min(max(requested, min_size), state.current_position_qty)


def main() -> int:
    parser = argparse.ArgumentParser(description="One-cycle controlled HL testnet layered TP probe")
    parser.add_argument("--config", required=True)
    parser.add_argument("--coin", required=True)
    parser.add_argument("--level", required=True, type=int)
    parser.add_argument("--qty", type=float, default=None)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--one-cycle", action="store_true")
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
    if args.level < 0 or args.level >= len(config.take_profit.get("layered_profit", {}).get("levels", [])):
        print("ERROR: invalid TP level")
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
        print("STOPPED_BEFORE_TP: local_state_load_terminal")
        return 1
    if state.pending_order is not None:
        print("STOPPED_BEFORE_TP: local_pending_order_exists")
        return 1
    if state.state not in {
        BotState.STARTER_LONG,
        BotState.BASE_LONG,
        BotState.PYRAMID_LONG,
        BotState.REDUCE_MODE,
        BotState.RUNNER_LONG,
        BotState.EVENT_RISK_MODE,
    }:
        print("STOPPED_BEFORE_TP: local_state_not_long")
        print("state=" + state.state.value)
        return 1
    if state.base_lot is None or state.current_position_qty <= EPSILON:
        print("STOPPED_BEFORE_TP: missing_position")
        return 1
    if state.tp_levels_triggered[args.level]:
        print("STOPPED_BEFORE_TP: tp_level_already_triggered")
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
        print(f"STOPPED_BEFORE_TP: open_orders_count={snapshot.open_orders_count}")
        return 1

    position = next((p for p in snapshot.positions if p.coin == coin), None)
    exchange_qty = position.qty if position is not None else 0.0
    if exchange_qty <= EPSILON:
        print("STOPPED_BEFORE_TP: no_exchange_position")
        return 1
    if abs(exchange_qty - state.current_position_qty) > EPSILON:
        print("STOPPED_BEFORE_TP: local_exchange_qty_mismatch")
        print("exchange_qty=" + str(exchange_qty))
        print("local_qty=" + str(state.current_position_qty))
        return 1

    recent_fills = get_recent_user_fills(wallet, client)
    state, reconciliation = reconcile_state_with_exchange(state, snapshot, recent_fills, config)
    if reconciliation.status.value == "halted" or state.halted:
        write_state(state, str(state_path))
        print("STOPPED_BEFORE_TP: reconciliation_halted")
        print("halt_reason=" + str(state.halt_reason))
        return 1
    if state.pending_order is not None:
        print("STOPPED_BEFORE_TP: pending_after_reconciliation")
        return 1

    mark_price = position.mark_price or position.entry_price
    indicators = _synthetic_indicators(mark_price)
    equity = snapshot.effective_trading_collateral or snapshot.account_value or float(config.capital["starting_equity"])
    mark_to_market_state(state, mark_price)

    sz_decimals = int(hl_cfg.get("sz_decimals", state.sz_decimals or 0) or 0)
    min_size = float(hl_cfg.get("min_size", 0.0) or 0.0)
    raw_qty = args.qty if args.qty is not None else _tp_level_qty(config, state, args.level, min_size)
    raw_qty = min(raw_qty, state.current_position_qty)
    qty_dec = format_hl_qty(raw_qty, sz_decimals, rounding="down")
    qty = float(qty_dec)
    if qty < min_size or qty <= EPSILON:
        print(f"STOPPED_BEFORE_TP: qty_below_min_size qty={decimal_to_plain_string(qty_dec)}")
        return 1

    tp_before = list(state.tp_levels_triggered)
    addon_qty_before = [lot.qty for lot in state.addon_lots]
    base_qty_before = state.base_lot.qty if state.base_lot is not None else 0.0
    state_before = state.state.value
    decision = Decision(
        action=ActionType.SELL_TAKE_PROFIT,
        qty=qty,
        reason=f"manual_controlled_layered_tp_level_{args.level + 1}",
        new_state=state.state,
    )
    print("pre_tp_state=" + state_before)
    print("tp_level=" + str(args.level))
    print("tp_triggered_before=" + str(tp_before))
    print(f"exchange_qty={exchange_qty}")
    print("tp_side=sell")
    print("reduce_only=True")
    print("raw_tp_qty=" + str(raw_qty))
    print("tp_qty=" + decimal_to_plain_string(qty_dec))
    print("base_qty_before=" + str(base_qty_before))
    print("addon_lots_before=" + str(addon_qty_before))

    # Keep TP probe pricing inside the same oracle-safe envelope used by cleanup
    # exits, without changing the repository config or strategy signal logic.
    execution_cfg = dict(config.execution)
    execution_cfg["limit_offset_bps"] = min(
        float(execution_cfg.get("limit_offset_bps", 10)),
        float(execution_cfg.get("exit_price_aggressiveness_bps", 2)),
    )
    probe_config = config.model_copy(update={"execution": execution_cfg})

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
            "take_profit_failed:"
            + (order_result.exchange_error_sanitized or order_result.raw_status_sanitized or order_result.status.value)
        )[:180]
        order_result.applied_state_after = state.state.value
        risk_status = "halted"
    elif order_result.status == OrderResultStatus.SUBMITTED_UNFILLED:
        state.pending_order = _pending_from_order_result(state, decision, order_result, tp_level=args.level)
        order_result.applied_state_after = state.state.value
        risk_status = "pending_reduce"
    elif fill is not None:
        apply_fill(state, fill)
        if state.current_position_qty <= EPSILON:
            state.state = BotState.EXITED
        elif state.addon_lots:
            state.state = BotState.PYRAMID_LONG
        else:
            state.state = BotState.BASE_LONG
        state.tp_levels_triggered[args.level] = True
        state.halted = False
        state.halt_reason = None
        order_result.applied_state_after = state.state.value
        if order_result.remaining_qty > EPSILON:
            state.pending_order = _pending_from_order_result(state, decision, order_result, tp_level=args.level)
            risk_status = "pending_reduce"
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
    print("tp_triggered_after=" + str(state.tp_levels_triggered))
    print("base_qty_after=" + str(state.base_lot.qty if state.base_lot is not None else None))
    print("addon_lots_after=" + str([lot.qty for lot in state.addon_lots]))
    print("avg_entry_price=" + str(state.avg_entry_price))
    print("realized_pnl=" + str(state.realized_pnl))
    print("halted=" + str(state.halted))
    print("halt_reason=" + str(state.halt_reason))
    print("pending_order=" + str(state.pending_order.oid if state.pending_order is not None else None))
    return 0 if not state.halted else 1


if __name__ == "__main__":
    raise SystemExit(main())
