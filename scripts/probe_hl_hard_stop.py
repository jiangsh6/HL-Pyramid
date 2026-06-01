"""One-cycle, controlled Hyperliquid hard-stop probe.

Probe-only validation path:
  - testnet only
  - requires an existing long position
  - synthesizes a hard-stop trigger
  - submits the resulting SELL_STOP through the normal HL broker path
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.probe_hl_starter_order import get_live_probe_prices
from src.core.config_loader import config_snapshot_hash, load_config
from src.core.decision_engine import run as engine_run
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
from src.reporting.logger import log_cycle
from src.reporting.state_writer import apply_fill, load_state_or_halt, mark_to_market_state, write_state


def _mask(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr
    return addr[:6] + "..." + addr[-3:]


def _synthetic_indicators(price: float) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        date=date.today(),
        adj_close=price,
        open=price,
        high=price,
        low=price,
        volume=0.0,
        prev_adj_close=price,
        ma5=price,
        ma10=price,
        ma20=price,
        ma50=price,
        atr14=max(price * 0.01, 1.0),
        avg_volume_20d=0.0,
        prior_highest_high_20d=price,
        drawdown_from_20d_high=0.0,
        distance_from_ma10=0.0,
        distance_from_ma20=0.0,
        intraday_return=0.0,
        gap_up_pct=0.0,
        gap_down_pct=0.0,
        close=price,
        bar_time=datetime.now(timezone.utc),
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


def apply_hard_stop_order_result(state, decision: Decision, order_result, *, is_ioc: bool):
    fill = _fill_from_order_result(order_result)
    risk_status = None

    ioc_no_match = (
        is_ioc
        and order_result.status == OrderResultStatus.REJECTED
        and "could not immediately match" in (order_result.exchange_error_sanitized or "").lower()
    )
    if ioc_no_match:
        state.state = BotState.HALTED
        state.halted = True
        state.halt_reason = "hard_stop_ioc_unfilled_position_still_open"
        state.pending_order = None
        order_result.applied_state_after = state.state.value
        return fill, "halted"

    if order_result.status in {
        OrderResultStatus.EXECUTION_ERROR,
        OrderResultStatus.REJECTED,
        OrderResultStatus.UNKNOWN,
        OrderResultStatus.CANCEL_FAILED,
    }:
        state.state = BotState.HALTED
        state.halted = True
        state.halt_reason = (
            "hard_stop_failed:"
            + (order_result.exchange_error_sanitized or order_result.raw_status_sanitized or order_result.status.value)
        )[:180]
        order_result.applied_state_after = state.state.value
        return fill, "halted"

    if order_result.status == OrderResultStatus.SUBMITTED_UNFILLED:
        state.pending_order = None if is_ioc else _pending_from_order_result(state, decision, order_result)
        if is_ioc:
            state.state = BotState.HALTED
            state.halted = True
            state.halt_reason = "hard_stop_ioc_unfilled_position_still_open"
            risk_status = "halted"
        else:
            state.halted = False
            state.halt_reason = None
            risk_status = "pending_exit"
        order_result.applied_state_after = state.state.value
        return fill, risk_status

    if fill is not None:
        apply_fill(state, fill)
        if state.current_position_qty <= EPSILON:
            state.state = BotState.EXITED
            state.halted = False
            state.halt_reason = None
            state.pending_order = None
            risk_status = "ok"
        else:
            state.pending_order = None if is_ioc else _pending_from_order_result(state, decision, order_result)
            if is_ioc:
                state.state = BotState.HALTED
                state.halted = True
                state.halt_reason = "hard_stop_ioc_partial_residual"
                risk_status = "halted"
            else:
                state.halted = False
                state.halt_reason = None
                risk_status = "pending_exit"
        order_result.applied_state_after = state.state.value

    return fill, risk_status


def main() -> int:
    parser = argparse.ArgumentParser(description="One-cycle controlled HL hard-stop probe")
    parser.add_argument("--config", required=True)
    parser.add_argument("--coin", required=True)
    parser.add_argument("--stop-loss-pct", type=float, required=True)
    parser.add_argument("--time-in-force", choices=("gtc", "ioc"), default="ioc")
    parser.add_argument("--exit-aggressiveness-bps", type=float, default=100.0)
    parser.add_argument("--max-oracle-deviation-bps", type=float, default=100.0)
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
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"

    print("network=testnet")
    print("mainnet_used=False")
    print("account_address=" + _mask(wallet))
    print("private_key_loaded=True")

    state, terminal = load_state_or_halt(str(state_path), config.symbol.get("ticker", coin))
    if terminal:
        print("STOPPED_BEFORE_HARD_STOP: local_state_load_terminal")
        return 1
    if state.pending_order is not None:
        print("STOPPED_BEFORE_HARD_STOP: local_pending_order_exists")
        return 1
    if state.state not in {BotState.STARTER_LONG, BotState.BASE_LONG, BotState.PYRAMID_LONG, BotState.RUNNER_LONG}:
        print("STOPPED_BEFORE_HARD_STOP: local_state_not_long")
        print("state=" + state.state.value)
        return 1
    if state.current_position_qty <= EPSILON:
        print("STOPPED_BEFORE_HARD_STOP: no_local_position")
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
        print(f"STOPPED_BEFORE_HARD_STOP: open_orders_count={snapshot.open_orders_count}")
        return 1
    position = next((p for p in snapshot.positions if p.coin == coin), None)
    exchange_qty = position.qty if position is not None else 0.0
    if exchange_qty <= EPSILON:
        print("STOPPED_BEFORE_HARD_STOP: no_exchange_position")
        return 1
    if abs(exchange_qty - state.current_position_qty) > EPSILON:
        print("STOPPED_BEFORE_HARD_STOP: local_exchange_qty_mismatch")
        print("exchange_qty=" + str(exchange_qty))
        print("local_qty=" + str(state.current_position_qty))
        return 1

    recent_fills = get_recent_user_fills(wallet, client)
    state, reconciliation = reconcile_state_with_exchange(state, snapshot, recent_fills, config)
    if reconciliation.status.value == "halted" or state.halted:
        write_state(state, str(state_path))
        print("STOPPED_BEFORE_HARD_STOP: reconciliation_halted")
        print("halt_reason=" + str(state.halt_reason))
        return 1

    live_mark_px, live_oracle_px = get_live_probe_prices(client, coin)
    mark_price = live_mark_px if live_mark_px is not None else live_oracle_px
    if mark_price is None:
        print("STOPPED_BEFORE_HARD_STOP: live_mark_or_oracle_price_unavailable")
        return 1

    trigger_price = (state.avg_entry_price or mark_price) * (1 - args.stop_loss_pct)
    synthetic_price = trigger_price * 0.999
    indicators = _synthetic_indicators(synthetic_price)
    mark_to_market_state(state, mark_price)
    state_before = state.state.value

    execution_cfg = dict(config.execution)
    execution_cfg.update(
        {
            "time_in_force": args.time_in_force,
            "exit_price_aggressiveness_bps": args.exit_aggressiveness_bps,
            "max_oracle_deviation_bps": args.max_oracle_deviation_bps,
        }
    )
    risk_cfg = dict(config.risk)
    risk_cfg["hard_stop"] = {"stop_loss_pct": args.stop_loss_pct}
    probe_config = config.model_copy(update={"execution": execution_cfg, "risk": risk_cfg})

    state_for_engine = state.model_copy(deep=True)
    decision = engine_run(state_for_engine, indicators, probe_config, hl_snapshot=snapshot)
    print("pre_stop_state=" + state_before)
    print("hard_stop_trigger_price=" + str(trigger_price))
    print("synthetic_price=" + str(synthetic_price))
    print("live_mark_price=" + str(mark_price))
    print("markPx=" + str(live_mark_px))
    print("oraclePx=" + str(live_oracle_px))
    print("decision_action=" + decision.action.value)
    print("decision_qty=" + str(decision.qty))
    print("decision_new_state=" + str(decision.new_state.value if decision.new_state else None))
    if decision.action != ActionType.SELL_STOP:
        print("STOPPED_BEFORE_HARD_STOP: sell_stop_not_generated")
        print("decision_reason=" + decision.reason)
        return 1

    order_result = execute_hl(
        decision,
        state,
        probe_config,
        client,
        wallet,
        private_key,
        mark_price,
    )
    fill, risk_status = apply_hard_stop_order_result(
        state,
        decision,
        order_result,
        is_ioc=(args.time_in_force.lower() == "ioc"),
    )
    mark_to_market_state(state, mark_price)
    write_state(state, str(state_path))
    equity = snapshot.effective_trading_collateral or snapshot.account_value or float(config.capital["starting_equity"])
    log_cycle(
        run_dir=config.logging.get("run_dir", "reports/btc_hl_testnet"),
        state=state,
        decision=decision,
        fill=fill,
        indicators=indicators,
        equity=equity,
        state_before=state_before,
        state_after=state.state.value,
        config_hash=config_snapshot_hash(probe_config),
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
    print("side=" + str(order_result.side))
    print("reduce_only=" + str(order_result.reduce_only))
    print("submitted_qty=" + str(order_result.submitted_qty))
    print("filled_qty=" + str(order_result.filled_qty))
    print("remaining_qty=" + str(order_result.remaining_qty))
    print("limit_px=" + str(order_result.limit_px))
    print("avg_fill_px=" + str(order_result.avg_fill_px))
    print("post_exchange_qty=" + str(post_qty))
    print("post_open_orders_count=" + str(post.open_orders_count))
    print("final_state=" + state.state.value)
    print("current_position_qty=" + str(state.current_position_qty))
    print("pending_order=" + str(state.pending_order.oid if state.pending_order is not None else None))
    print("halted=" + str(state.halted))
    print("halt_reason=" + str(state.halt_reason))
    return 0 if order_result.status == OrderResultStatus.FILLED and state.state == BotState.EXITED else 1


if __name__ == "__main__":
    raise SystemExit(main())
