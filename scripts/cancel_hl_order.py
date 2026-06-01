"""One-cycle, exact-oid Hyperliquid order cancel/reconcile utility.

This script is deliberately narrow for testnet cleanup:
  - testnet only
  - exact coin + oid only
  - no new orders
  - no recurring loop
  - fill reconciliation before cancel
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
    ReconciliationStatus,
)
from src.execution.operator_recovery import (
    DUST_EXIT_PENDING_ACTION,
    is_target_runner_pending,
    pending_missing_reason,
    pending_reconcile_result_text,
    runner_target_from_pending,
)
from src.execution.reconciler import reconcile_state_with_exchange
from src.hl.account import get_account_snapshot, get_recent_user_fills
from src.hl.auth import get_private_key
from src.hl.client import HyperliquidClient
from src.hl.order_placer import cancel_order
from src.reporting.logger import log_cycle
from src.reporting.state_writer import load_state_or_halt, mark_to_market_state, write_state


def _tp_level_from_pending(pending) -> int | None:
    source = pending.source_decision_id or ""
    prefix = "tp_level:"
    if not source.startswith(prefix):
        return None
    try:
        level = int(source[len(prefix):])
    except ValueError:
        return None
    return level if 0 <= level < 4 else None


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


def _log(
    *,
    config,
    state,
    decision,
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Cancel or reconcile one exact HL testnet order")
    parser.add_argument("--config", required=True)
    parser.add_argument("--coin", required=True)
    parser.add_argument("--oid", required=True, type=int)
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
        print("STOPPED: local_state_load_terminal")
        return 1
    pending = state.pending_order
    if pending is None or pending.oid != args.oid:
        print("STOPPED: local_pending_order_oid_mismatch")
        print("local_pending_oid=" + str(pending.oid if pending is not None else None))
        return 1

    snapshot = get_account_snapshot(
        wallet,
        client,
        include_spot=True,
        include_subaccounts=True,
        include_open_orders=True,
        collateral_mode=hl_cfg.get("collateral_mode", "unified"),
    )
    position = next((p for p in snapshot.positions if p.coin == coin), None)
    exchange_qty = position.qty if position is not None else 0.0
    mark_price = (position.mark_price if position is not None else 0.0) or pending.limit_px
    indicators = _synthetic_indicators(mark_price)
    equity = snapshot.effective_trading_collateral or snapshot.account_value or float(config.capital["starting_equity"])
    open_orders = [order for order in snapshot.open_orders if order.coin == coin]
    target_open = [order for order in open_orders if order.oid == args.oid]
    recent_fills = get_recent_user_fills(wallet, client)
    fills_for_oid = [f for f in recent_fills if f.oid == args.oid]

    print("pre_exchange_qty=" + str(exchange_qty))
    print("pre_open_orders_count=" + str(snapshot.open_orders_count))
    print("target_oid_open=" + str(bool(target_open)))
    print("recent_fills_for_oid=" + str(len(fills_for_oid)))

    state_before = state.state.value
    state, reconciliation = reconcile_state_with_exchange(state, snapshot, recent_fills, config)
    if reconciliation.status in {
        ReconciliationStatus.CLEARED_PENDING_ORDER,
        ReconciliationStatus.RECONSTRUCTED_POSITION,
    }:
        if pending.action == "sell_take_profit":
            tp_level = _tp_level_from_pending(pending)
            if tp_level is not None:
                state.tp_levels_triggered[tp_level] = True
            runner_target = runner_target_from_pending(pending)
            if runner_target is not None and state.current_position_qty <= runner_target + EPSILON:
                state.runner_target_qty = runner_target
                state.runner_mode_active = True
                state.target_price_tp_triggered = True
                state.state = BotState.RUNNER_LONG
                state.halted = False
                state.halt_reason = None
        if pending.action == DUST_EXIT_PENDING_ACTION and state.current_position_qty <= EPSILON:
            state.dust_position = False
            state.dust_qty = 0.0
            state.dust_notional = 0.0
            state.dust_reason = None
        mark_to_market_state(state, mark_price)
        write_state(state, str(state_path))
        result_text = pending_reconcile_result_text(pending)
        if pending.action in {"buy_starter", "buy_base", "buy_addon"}:
            decision = Decision(
                action=ActionType.RECONCILE_POSITION,
                qty=0.0,
                reason="pending_entry_filled_reconciled",
                new_state=state.state,
            )
        elif pending.action in {"sell_reduce_addon", "sell_reduce_base", "sell_take_profit", "sell_event_derisking"}:
            reason = "pending_target_runner_filled_reconciled" if is_target_runner_pending(pending) else "pending_reduce_filled_reconciled"
            decision = Decision(
                action=ActionType.RECONCILE_POSITION,
                qty=0.0,
                reason=reason,
                new_state=state.state,
            )
        elif pending.action == DUST_EXIT_PENDING_ACTION:
            decision = Decision(
                action=ActionType.EXIT_ALL,
                qty=pending.qty_remaining,
                reason=result_text,
                new_state=state.state,
            )
        else:
            decision = Decision(
                action=ActionType.EXIT_ALL,
                qty=pending.qty_remaining,
                reason="pending_exit_filled_reconciled",
                new_state=state.state,
            )
        _log(
            config=config,
            state=state,
            decision=decision,
            indicators=indicators,
            equity=equity,
            state_before=state_before,
            reconciliation_result=reconciliation,
            risk_status_override="reconciled",
        )
        print("ACTION=reconcile_only")
        print("result=" + result_text)
    elif not target_open:
        state.state = BotState.HALTED
        state.halted = True
        state.halt_reason = pending_missing_reason(pending)
        write_state(state, str(state_path))
        decision = Decision(action=ActionType.NO_ACTION, reason=state.halt_reason)
        _log(
            config=config,
            state=state,
            decision=decision,
            indicators=indicators,
            equity=equity,
            state_before=state_before,
            reconciliation_result=reconciliation,
            risk_status_override="halted",
        )
        print("ACTION=no_action")
        print("result=" + state.halt_reason)
        return 1
    else:
        ok = cancel_order(coin, args.oid, wallet, private_key, client)
        post = get_account_snapshot(
            wallet,
            client,
            include_spot=True,
            include_subaccounts=False,
            include_open_orders=True,
            collateral_mode=hl_cfg.get("collateral_mode", "unified"),
        )
        if ok:
            state.pending_order = None
            if pending.action in {"buy_starter", "buy_base", "buy_addon"} and exchange_qty <= EPSILON:
                state.state = BotState.FLAT
                state.halted = False
                state.halt_reason = None
            elif pending.action in {"sell_reduce_addon", "sell_reduce_base", "sell_take_profit", "sell_event_derisking"}:
                state.halted = False
                state.halt_reason = None
            elif pending.action == DUST_EXIT_PENDING_ACTION:
                state.state = BotState.HALTED
                state.halted = True
                state.halt_reason = "dust_exit_order_canceled_position_still_open"
            else:
                state.state = BotState.HALTED
                state.halted = True
                state.halt_reason = "exit_order_canceled_position_still_open"
            mark_to_market_state(state, mark_price)
            status = OrderResultStatus.CANCELED
            risk_status = "halted"
            print("ACTION=cancel_oid")
            print("result=cancel_success")
        else:
            state.state = BotState.HALTED
            state.halted = True
            state.halt_reason = "cancel_failed"
            status = OrderResultStatus.CANCEL_FAILED
            risk_status = "halted"
            print("ACTION=cancel_oid")
            print("result=cancel_failed")
        order_result = OrderResult(
            status=status,
            oid=args.oid,
            client_order_id=pending.client_order_id,
            action=ActionType.EXIT_ALL,
            side="sell",
            reduce_only=True,
            submitted_qty=pending.qty_submitted,
            filled_qty=0.0,
            remaining_qty=pending.qty_remaining,
            limit_px=pending.limit_px,
            raw_status_sanitized=status.value,
            state_before=state_before,
            intended_state_after=pending.intended_state_after,
            applied_state_after=state.state.value,
            created_at=datetime.now(timezone.utc),
        )
        write_state(state, str(state_path))
        decision = Decision(action=ActionType.EXIT_ALL, qty=pending.qty_remaining, reason="cancel_pending_exit_order")
        _log(
            config=config,
            state=state,
            decision=decision,
            indicators=indicators,
            equity=equity,
            state_before=state_before,
            order_result=order_result,
            reconciliation_result=reconciliation,
            risk_status_override=risk_status,
        )
        post_position = next((p for p in post.positions if p.coin == coin), None)
        print("post_exchange_qty=" + str(post_position.qty if post_position is not None else 0.0))
        print("post_open_orders_count=" + str(post.open_orders_count))

    final_snapshot = get_account_snapshot(
        wallet,
        client,
        include_spot=False,
        include_subaccounts=False,
        include_open_orders=True,
        collateral_mode=hl_cfg.get("collateral_mode", "unified"),
    )
    final_position = next((p for p in final_snapshot.positions if p.coin == coin), None)
    print("final_exchange_qty=" + str(final_position.qty if final_position is not None else 0.0))
    print("final_open_orders_count=" + str(final_snapshot.open_orders_count))
    print("final_state=" + state.state.value)
    print("halted=" + str(state.halted))
    print("halt_reason=" + str(state.halt_reason))
    print("pending_order=" + str(state.pending_order.oid if state.pending_order is not None else None))
    return 0 if state.halt_reason != "cancel_failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
