"""One-cycle, controlled Hyperliquid target-price TP / runner-mode probe."""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

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
    get_probe_runner_pct,
    validate_runner_probe_sizes,
)
from src.execution.reconciler import reconcile_state_with_exchange
from src.hl.account import get_account_snapshot, get_recent_user_fills
from src.hl.auth import get_private_key
from src.hl.client import HyperliquidClient
from src.hl.precision import decimal_to_plain_string, format_hl_qty
from src.hl.pricing import compute_oracle_safe_exit_price
from src.reporting.logger import log_cycle
from src.reporting.state_writer import apply_fill, load_state_or_halt, mark_to_market_state, write_state

TARGET_RUNNER_RECOVERY_HALT_REASON = "exit_order_canceled_position_still_open"


def _mask(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr
    return addr[:6] + "..." + addr[-3:]


def _as_optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def get_live_probe_prices(client: HyperliquidClient, coin: str) -> tuple[float | None, float | None]:
    response = client.post_info({"type": "metaAndAssetCtxs"})
    if not isinstance(response, list) or len(response) < 2:
        return None, None

    meta, asset_ctxs = response[0], response[1]
    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    if not isinstance(universe, list) or not isinstance(asset_ctxs, list):
        return None, None

    for idx, asset in enumerate(universe):
        if not isinstance(asset, dict) or asset.get("name") != coin:
            continue
        if idx >= len(asset_ctxs) or not isinstance(asset_ctxs[idx], dict):
            return None, None
        ctx = asset_ctxs[idx]
        return (
            _as_optional_float(ctx.get("markPx")),
            _as_optional_float(ctx.get("oraclePx")),
        )
    return None, None


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


def _pending_from_order_result(state, decision: Decision, order_result, *, runner_target_qty: float) -> PendingOrder:
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
        source_decision_id=f"target_runner:{runner_target_qty}",
        state_before=order_result.state_before,
        intended_state_after=order_result.intended_state_after,
        applied_state_after=order_result.applied_state_after,
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


def compute_runner_quantities(state, runner_pct: float, min_size: float) -> tuple[float, float, str | None]:
    return validate_runner_probe_sizes(
        state.original_base_qty,
        state.current_position_qty,
        runner_pct,
        min_size,
    )


def validate_halted_target_runner_recovery(
    state,
    *,
    exchange_qty: float,
    open_orders_count: int,
    reduce_qty: float,
    min_size: float,
) -> str | None:
    if state.state != BotState.HALTED:
        return "recovery_state_not_halted"
    if state.halt_reason != TARGET_RUNNER_RECOVERY_HALT_REASON:
        return "recovery_wrong_halt_reason"
    if state.pending_order is not None:
        return "recovery_pending_order_exists"
    if open_orders_count != 0:
        return f"recovery_open_orders_count={open_orders_count}"
    if exchange_qty <= EPSILON:
        return "recovery_no_exchange_position"
    if state.current_position_qty <= EPSILON:
        return "recovery_no_local_position"
    if state.original_base_qty <= EPSILON:
        return "recovery_original_base_qty_missing"
    if state.target_price_tp_triggered:
        return "recovery_target_price_already_triggered"
    if state.runner_mode_active:
        return "recovery_runner_mode_already_active"
    if reduce_qty > exchange_qty + EPSILON:
        return "recovery_reduce_qty_exceeds_exchange_position"
    if reduce_qty < min_size or reduce_qty <= EPSILON:
        return "recovery_reduce_qty_below_min_size"
    return None


def resolve_probe_target_runner_pricing(
    *,
    config,
    mark_price: float,
    oracle_price: float | None,
    sz_decimals: int,
    pricing_mode: str,
    aggressiveness_bps: float,
    max_oracle_deviation_bps: float,
    max_aggressiveness_bps: float,
    time_in_force: str,
):
    raw_probe_px = mark_price * (1 - float(config.execution.get("limit_offset_bps", 10)) / 10_000.0)
    formatted_probe_px = raw_probe_px
    if pricing_mode == "passive":
        execution_cfg = dict(config.execution)
        execution_cfg.update({"limit_offset_bps": 0, "time_in_force": time_in_force})
        return {
            "probe_config": config.model_copy(update={"execution": execution_cfg}),
            "execution_mark_price": raw_probe_px,
            "raw_probe_px": raw_probe_px,
            "formatted_probe_px": formatted_probe_px,
            "blocker": None,
        }

    if aggressiveness_bps > max_aggressiveness_bps:
        return {
            "probe_config": config,
            "execution_mark_price": mark_price,
            "raw_probe_px": raw_probe_px,
            "formatted_probe_px": formatted_probe_px,
            "blocker": "target_runner_aggressiveness_exceeds_max",
        }
    if pricing_mode == "cross" and aggressiveness_bps > max_oracle_deviation_bps:
        return {
            "probe_config": config,
            "execution_mark_price": mark_price,
            "raw_probe_px": raw_probe_px,
            "formatted_probe_px": formatted_probe_px,
            "blocker": "target_runner_cross_exceeds_max_oracle_deviation",
        }

    oracle_guard_price = oracle_price
    if oracle_guard_price is not None and oracle_guard_price > mark_price:
        oracle_guard_price = mark_price
    price_info = compute_oracle_safe_exit_price(
        "sell",
        mark_price=mark_price,
        oracle_price=oracle_guard_price,
        sz_decimals=sz_decimals,
        max_oracle_deviation_bps=max_oracle_deviation_bps,
        aggressiveness_bps=aggressiveness_bps,
        is_perp=True,
    )
    execution_cfg = dict(config.execution)
    execution_cfg.update({"limit_offset_bps": 0, "time_in_force": time_in_force})
    formatted_probe_px = price_info.formatted_exit_px
    if formatted_probe_px > price_info.raw_exit_px:
        step = 10 ** (Decimal(str(formatted_probe_px)).adjusted() - 4)
        formatted_probe_px = float(Decimal(str(formatted_probe_px)) - Decimal(str(step)))
    return {
        "probe_config": config.model_copy(update={"execution": execution_cfg}),
        "execution_mark_price": formatted_probe_px,
        "raw_probe_px": price_info.raw_exit_px,
        "formatted_probe_px": formatted_probe_px,
        "blocker": None,
    }


def apply_target_runner_order_result(
    state,
    decision: Decision,
    order_result,
    *,
    runner_target_qty: float,
    min_size: float,
    is_ioc: bool,
) -> tuple[object | None, str | None]:
    fill = _fill_from_order_result(order_result)
    risk_status = None

    ioc_unfilled_rejection = (
        is_ioc
        and order_result.status == OrderResultStatus.REJECTED
        and "could not immediately match" in (order_result.exchange_error_sanitized or "").lower()
    )
    if ioc_unfilled_rejection:
        state.pending_order = None
        order_result.applied_state_after = state.state.value
        return fill, "blocked"

    if order_result.status in {
        OrderResultStatus.EXECUTION_ERROR,
        OrderResultStatus.REJECTED,
        OrderResultStatus.UNKNOWN,
        OrderResultStatus.CANCEL_FAILED,
    }:
        state.state = BotState.HALTED
        state.halted = True
        state.halt_reason = (
            "target_runner_failed:"
            + (order_result.exchange_error_sanitized or order_result.raw_status_sanitized or order_result.status.value)
        )[:180]
        order_result.applied_state_after = state.state.value
        return fill, "halted"

    if order_result.status == OrderResultStatus.SUBMITTED_UNFILLED:
        state.pending_order = None if is_ioc else _pending_from_order_result(
            state,
            decision,
            order_result,
            runner_target_qty=runner_target_qty,
        )
        order_result.applied_state_after = state.state.value
        return fill, "blocked" if is_ioc else "pending_reduce"

    if fill is not None:
        apply_fill(state, fill)
        state.pending_order = None
        if order_result.status == OrderResultStatus.FILLED and state.current_position_qty <= runner_target_qty + EPSILON:
            state.state = BotState.RUNNER_LONG
            state.runner_target_qty = runner_target_qty
            state.runner_mode_active = True
            state.target_price_tp_triggered = True
            state.halted = False
            state.halt_reason = None
            risk_status = "ok"
        elif is_ioc:
            state.state = BotState.HALTED
            state.halted = True
            state.halt_reason = (
                "target_runner_ioc_partial_runner_qty_below_min_size"
                if state.current_position_qty < min_size - EPSILON
                else "target_runner_ioc_partial_manual_review"
            )
            risk_status = "halted"
        else:
            state.pending_order = _pending_from_order_result(
                state,
                decision,
                order_result,
                runner_target_qty=runner_target_qty,
            )
            risk_status = "pending_reduce"
        order_result.applied_state_after = state.state.value

    return fill, risk_status


def main() -> int:
    parser = argparse.ArgumentParser(description="One-cycle controlled HL target runner probe")
    parser.add_argument("--config", required=True)
    parser.add_argument("--coin", required=True)
    parser.add_argument("--runner-pct", type=float, default=None)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--one-cycle", action="store_true")
    parser.add_argument("--allow-min-size-edge-test", action="store_true")
    parser.add_argument("--pricing-mode", choices=("passive", "aggressive", "cross"), default=None)
    parser.add_argument("--aggressiveness-bps", type=float, default=None)
    parser.add_argument("--max-aggressiveness-bps", type=float, default=None)
    parser.add_argument("--time-in-force", choices=("gtc", "ioc"), default=None)
    parser.add_argument("--allow-halted-target-runner-recovery", action="store_true")
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
    runner_pct = get_probe_runner_pct(config, args.runner_pct)

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
        print("STOPPED_BEFORE_TARGET_RUNNER: local_state_load_terminal")
        return 1
    recovery_requested = bool(args.allow_halted_target_runner_recovery)
    if state.pending_order is not None and not recovery_requested:
        print("STOPPED_BEFORE_TARGET_RUNNER: local_pending_order_exists")
        return 1
    if (
        not recovery_requested
        and state.state not in {BotState.STARTER_LONG, BotState.BASE_LONG, BotState.PYRAMID_LONG}
    ):
        print("STOPPED_BEFORE_TARGET_RUNNER: local_state_not_long")
        print("state=" + state.state.value)
        return 1
    if state.base_lot is None or state.current_position_qty <= EPSILON:
        print("STOPPED_BEFORE_TARGET_RUNNER: missing_position")
        return 1
    if state.target_price_tp_triggered and not recovery_requested:
        print("STOPPED_BEFORE_TARGET_RUNNER: target_price_already_triggered")
        return 1

    snapshot = get_account_snapshot(
        wallet,
        client,
        include_spot=True,
        include_subaccounts=True,
        include_open_orders=True,
        collateral_mode=hl_cfg.get("collateral_mode", "unified"),
    )
    if snapshot.open_orders_count > 0 and not recovery_requested:
        print(f"STOPPED_BEFORE_TARGET_RUNNER: open_orders_count={snapshot.open_orders_count}")
        return 1
    position = next((p for p in snapshot.positions if p.coin == coin), None)
    exchange_qty = position.qty if position is not None else 0.0
    if exchange_qty <= EPSILON:
        print("STOPPED_BEFORE_TARGET_RUNNER: no_exchange_position")
        return 1
    if abs(exchange_qty - state.current_position_qty) > EPSILON:
        print("STOPPED_BEFORE_TARGET_RUNNER: local_exchange_qty_mismatch")
        print("exchange_qty=" + str(exchange_qty))
        print("local_qty=" + str(state.current_position_qty))
        return 1

    recent_fills = get_recent_user_fills(wallet, client)
    state, reconciliation = reconcile_state_with_exchange(state, snapshot, recent_fills, config)
    if reconciliation.status.value == "halted" or (state.halted and not recovery_requested):
        write_state(state, str(state_path))
        print("STOPPED_BEFORE_TARGET_RUNNER: reconciliation_halted")
        print("halt_reason=" + str(state.halt_reason))
        return 1
    if state.pending_order is not None:
        print("STOPPED_BEFORE_TARGET_RUNNER: pending_after_reconciliation")
        return 1

    live_mark_px, live_oracle_px = get_live_probe_prices(client, coin)
    mark_price = live_mark_px if live_mark_px is not None else live_oracle_px
    if mark_price is None:
        print("STOPPED_BEFORE_TARGET_RUNNER: live_mark_or_oracle_price_unavailable")
        print("markPx=None")
        print("oraclePx=None")
        return 1
    indicators = _synthetic_indicators(mark_price)
    equity = snapshot.effective_trading_collateral or snapshot.account_value or float(config.capital["starting_equity"])
    mark_to_market_state(state, mark_price)

    min_size = float(hl_cfg.get("min_size", 0.0) or 0.0)
    runner_target_qty, reduce_qty, blocker = compute_runner_quantities(state, runner_pct, min_size)
    print("runner_pct=" + str(runner_pct))
    print("original_base_qty=" + str(state.original_base_qty))
    print("runner_target_qty=" + str(runner_target_qty))
    print("raw_reduce_qty=" + str(reduce_qty))
    if blocker is not None:
        print("STOPPED_BEFORE_TARGET_RUNNER: " + blocker)
        return 1

    sz_decimals = int(hl_cfg.get("sz_decimals", state.sz_decimals or 0) or 0)
    qty_dec = format_hl_qty(reduce_qty, sz_decimals, rounding="down")
    qty = float(qty_dec)
    if qty < min_size or qty <= EPSILON:
        print(f"STOPPED_BEFORE_TARGET_RUNNER: reduce_qty_below_min_size qty={decimal_to_plain_string(qty_dec)}")
        return 1
    if state.current_position_qty - qty < min_size - EPSILON:
        print("STOPPED_BEFORE_TARGET_RUNNER: runner_residual_below_min_size")
        return 1
    if recovery_requested:
        blocker = validate_halted_target_runner_recovery(
            state,
            exchange_qty=exchange_qty,
            open_orders_count=snapshot.open_orders_count,
            reduce_qty=qty,
            min_size=min_size,
        )
        if blocker is not None:
            print("STOPPED_BEFORE_TARGET_RUNNER: " + blocker)
            return 1

    probe_cfg = config.probe or {}
    pricing_mode = args.pricing_mode or str(probe_cfg.get("target_runner_pricing_mode", "passive"))
    aggressiveness_bps = float(
        args.aggressiveness_bps
        if args.aggressiveness_bps is not None
        else probe_cfg.get("target_runner_aggressiveness_bps", config.execution.get("limit_offset_bps", 10))
    )
    max_oracle_deviation_bps = float(
        probe_cfg.get(
            "max_oracle_deviation_bps",
            config.execution.get("max_oracle_deviation_bps", 20),
        )
    )
    max_aggressiveness_bps = float(
        args.max_aggressiveness_bps
        if args.max_aggressiveness_bps is not None
        else probe_cfg.get("max_aggressiveness_bps", max_oracle_deviation_bps)
    )
    time_in_force = args.time_in_force or str(probe_cfg.get("target_runner_time_in_force", "gtc"))
    pricing = resolve_probe_target_runner_pricing(
        config=config,
        mark_price=mark_price,
        oracle_price=live_oracle_px,
        sz_decimals=sz_decimals,
        pricing_mode=pricing_mode,
        aggressiveness_bps=aggressiveness_bps,
        max_oracle_deviation_bps=max_oracle_deviation_bps,
        max_aggressiveness_bps=max_aggressiveness_bps,
        time_in_force=time_in_force,
    )
    if pricing["blocker"] is not None:
        print("STOPPED_BEFORE_TARGET_RUNNER: " + str(pricing["blocker"]))
        return 1
    raw_probe_px = pricing["raw_probe_px"]
    formatted_probe_px = pricing["formatted_probe_px"]
    probe_config = pricing["probe_config"]
    execution_mark_price = pricing["execution_mark_price"]

    state_before = state.state.value
    decision = Decision(
        action=ActionType.SELL_TAKE_PROFIT,
        qty=qty,
        reason="manual_controlled_target_tp_runner_mode",
        new_state=BotState.RUNNER_LONG,
    )
    print("pre_runner_state=" + state_before)
    print("runner_side=sell")
    print("reduce_only=True")
    print("runner_reduce_qty=" + decimal_to_plain_string(qty_dec))
    print("pricing_mode=" + pricing_mode)
    print("aggressiveness_bps=" + str(aggressiveness_bps))
    print("max_aggressiveness_bps=" + str(max_aggressiveness_bps))
    print("time_in_force=" + time_in_force)
    print("mark_price=" + str(mark_price))
    print("markPx=" + str(live_mark_px))
    print("oraclePx=" + str(live_oracle_px))
    print("raw_probe_px=" + str(raw_probe_px))
    print("formatted_probe_px=" + str(formatted_probe_px))
    print("max_oracle_deviation_bps=" + str(max_oracle_deviation_bps))

    order_result = execute_hl(
        decision,
        state,
        probe_config,
        client,
        wallet,
        private_key,
        execution_mark_price,
    )
    fill, risk_status = apply_target_runner_order_result(
        state,
        decision,
        order_result,
        runner_target_qty=runner_target_qty,
        min_size=min_size,
        is_ioc=(time_in_force.lower() == "ioc"),
    )

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
    print("target_price_tp_triggered=" + str(state.target_price_tp_triggered))
    print("runner_mode_active=" + str(state.runner_mode_active))
    print("runner_target_qty=" + str(state.runner_target_qty))
    print("current_position_qty=" + str(state.current_position_qty))
    print("base_lot=" + str(state.base_lot.qty if state.base_lot is not None else None))
    print("addon_lots=" + str([lot.qty for lot in state.addon_lots]))
    print("avg_entry_price=" + str(state.avg_entry_price))
    print("pending_order=" + str(state.pending_order.oid if state.pending_order is not None else None))
    print("halted=" + str(state.halted))
    print("halt_reason=" + str(state.halt_reason))
    return 0 if not state.halted else 1


if __name__ == "__main__":
    raise SystemExit(main())
