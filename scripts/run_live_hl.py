"""
HL Phase 5 — 24/7 Live Loop (Hyperliquid Testnet / Mainnet).

Startup sequence:
  1. Load config and validate (mainnet gate enforced before any network call)
  2. Check the network-specific agent private key env var is set
  3. Build HyperliquidClient (testnet by default)
  4. Load state.json if present, else initialize fresh ThesisState
  5. Start WebSocket feed + intraday monitor thread
  6. Enter 4h candle-close decision loop

Shutdown: SIGTERM / SIGINT sets the global stop event, which unwinds the loop
and the intraday monitor thread within a few seconds.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Allow running as a script: project root must be on sys.path.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.config_loader import config_snapshot_hash, load_config
from src.core.decision_engine import run as engine_run
from src.core.models import (
    ActionClass,
    ActionType,
    BotConfig,
    BotState,
    Decision,
    Fill,
    OrderResult,
    OrderResultStatus,
    PendingOrder,
    ThesisState,
)
from src.data.indicators import calc_indicators
from src.execution.hl_broker import HLExecutionError, execute_hl
from src.execution import paper_broker
from src.execution.lifecycle import (
    classify_action,
    is_residual_emergency_exit,
    is_unfilled_emergency_exit,
)
from src.execution.reconciler import reconcile_state_with_exchange
from src.hl.account import get_account_snapshot, get_recent_user_fills
from src.hl.auth import get_private_key, validate_mainnet_intent
from src.hl.candles import fetch_ohlcv_hl
from src.hl.client import HyperliquidClient
from src.hl.funding import (
    apply_funding_to_state,
    calc_funding_payment,
    get_predicted_funding,
)
from src.hl.ws_feed import MAINNET_WS_URL, TESTNET_WS_URL, HLWebSocketFeed
from src.reporting.daily_summary import format_summary
from src.reporting.logger import log_cycle
from src.reporting.state_writer import (
    apply_fill,
    load_state_or_halt,
    write_state,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
_log = logging.getLogger("run_live_hl")

_STOP_EVENT = threading.Event()
_4H_SECONDS = 4 * 60 * 60
HIGH_FUNDING_RATE_THRESHOLD = 0.0005
CONTINUE = "CONTINUE"
HALTED = "HALTED"
EXECUTION_ERROR = "EXECUTION_ERROR"
STOPPED = "STOPPED"
BLOCKED_NO_COLLATERAL = "BLOCKED_NO_COLLATERAL"
BLOCKED_EXISTING_OPEN_ORDER = "BLOCKED_EXISTING_OPEN_ORDER"
RECONCILED = "RECONCILED"
STALE_PENDING_ORDER = "STALE_PENDING_ORDER"

_BUY_ACTION_TYPES = frozenset({
    ActionType.BUY_STARTER,
    ActionType.BUY_BASE,
    ActionType.BUY_ADDON,
})
_SELL_ACTION_TYPES = frozenset({
    ActionType.SELL_REDUCE_ADDON,
    ActionType.SELL_REDUCE_BASE,
    ActionType.SELL_TAKE_PROFIT,
    ActionType.SELL_STOP,
    ActionType.SELL_TRAILING_STOP,
    ActionType.SELL_EVENT_DERISKING,
    ActionType.EXIT_ALL,
})


# ── Time helpers ──────────────────────────────────────────────────────────────

def next_4h_candle_close(now: Optional[datetime] = None) -> datetime:
    """Return the next UTC 4h candle close after *now* (00:00/04:00/08:00/...)."""
    if now is None:
        now = datetime.now(timezone.utc)
    epoch_seconds = now.timestamp()
    next_boundary = (int(epoch_seconds // _4H_SECONDS) + 1) * _4H_SECONDS
    return datetime.fromtimestamp(next_boundary, tz=timezone.utc)


# ── Startup / shutdown ────────────────────────────────────────────────────────

def startup_checks(config: BotConfig) -> str:
    """Validate environment; return the private key. Raises SystemExit on failure."""
    if not config.hl:
        _fatal("Config missing 'hl' block — cannot start live loop")
    try:
        return get_private_key(config)
    except ValueError as exc:
        _fatal(str(exc))
        raise  # unreachable, but keeps type checker happy


def _fatal(msg: str) -> None:
    _log.critical("STARTUP FATAL: %s", msg)
    sys.exit(1)


def _install_shutdown_handler(stop_event: threading.Event) -> None:
    def _handler(signum, frame):  # noqa: ANN001
        _log.info("Signal %s received — initiating graceful shutdown", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


# ── Funding (preserved from user edit) ────────────────────────────────────────

def apply_candle_close_funding(
    state: ThesisState,
    coin: str,
    client: HyperliquidClient,
    mark_price: float,
    hours: float = 4.0,
) -> ThesisState:
    """Fetch predicted funding, warn if elevated, apply candle funding PnL."""
    predicted_rate = get_predicted_funding(coin, client)
    if predicted_rate is not None:
        state.last_funding_rate = predicted_rate
        if predicted_rate > HIGH_FUNDING_RATE_THRESHOLD:
            _log.warning("High funding rate: %.4f%%/hr", predicted_rate * 100)
    payment = calc_funding_payment(
        predicted_rate or 0.0,
        state.current_position_qty,
        mark_price,
        hours=hours,
    )
    if payment != 0.0:
        return apply_funding_to_state(state, payment)
    return state


# ── Broker dispatch ───────────────────────────────────────────────────────────

def _dispatch_broker(
    decision: Decision,
    state: ThesisState,
    config: BotConfig,
    client: HyperliquidClient,
    wallet_address: str,
    private_key: str,
    mark_price: float,
):
    """
    Route the Decision to the correct broker based on bot.mode.

    Returns the Fill produced by the broker. Raises on execution error.
    """
    mode = config.bot.get("mode", "paper")
    if mode == "paper":
        fill = paper_broker.execute(decision, mark_price, state, config)
        return OrderResult(
            status=OrderResultStatus.FILLED,
            action=decision.action,
            side="buy" if decision.action in _BUY_ACTION_TYPES else "sell",
            reduce_only=decision.action not in _BUY_ACTION_TYPES,
            submitted_qty=fill.qty,
            filled_qty=fill.qty,
            remaining_qty=0.0,
            avg_fill_px=fill.fill_price,
            limit_px=fill.fill_price,
            realized_pnl=fill.realized_pnl,
            commission=fill.commission,
            raw_status_sanitized="paper_fill",
            state_before=state.state.value,
            intended_state_after=decision.new_state.value if decision.new_state is not None else None,
            applied_state_after=decision.new_state.value if decision.new_state is not None else state.state.value,
            created_at=fill.timestamp,
        )
    # testnet / mainnet → HL broker (signs and submits)
    return execute_hl(
        decision, state, config, client, wallet_address, private_key, mark_price
    )


def _order_result_to_fill(order_result: OrderResult) -> Optional[Fill]:
    if order_result.filled_qty <= 0.0:
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


def _build_pending_order(
    state: ThesisState,
    decision: Decision,
    order_result: OrderResult,
) -> PendingOrder:
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


def _is_ioc_execution(config: BotConfig) -> bool:
    return str((config.execution or {}).get("time_in_force", "gtc")).strip().lower() == "ioc"


def pending_order_age_minutes(pending: PendingOrder, *, now: Optional[datetime] = None) -> Optional[float]:
    created = pending.created_at
    if created is None:
        return None
    now = now or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max((now - created).total_seconds() / 60.0, 0.0)


def pending_order_max_age_minutes(config: BotConfig) -> Optional[float]:
    raw = (config.execution or {}).get("pending_order_max_age_minutes")
    if raw is None:
        return None
    value = float(raw)
    return value if value > 0 else None


def mark_stale_pending_order_if_needed(
    state: ThesisState,
    config: BotConfig,
    *,
    now: Optional[datetime] = None,
) -> bool:
    pending = state.pending_order
    max_age = pending_order_max_age_minutes(config)
    if pending is None or max_age is None:
        return False
    age = pending_order_age_minutes(pending, now=now)
    if age is None or age <= max_age:
        return False
    pending.status = "stale"
    pending.last_checked_at = now or datetime.now(timezone.utc)
    state.state = BotState.HALTED
    state.halted = True
    state.halt_reason = "stale_pending_order_requires_cancel_or_reconcile"
    return True


def _normalize_dispatch_result(
    result,
    decision: Decision,
    state: ThesisState,
) -> OrderResult:
    if isinstance(result, OrderResult):
        return result
    if isinstance(result, Fill):
        status = OrderResultStatus.FILLED if result.qty > 0 else OrderResultStatus.SUBMITTED_UNFILLED
        return OrderResult(
            status=status,
            oid=result.order_id,
            action=result.action,
            side="buy" if result.action in _BUY_ACTION_TYPES else "sell",
            reduce_only=result.action in _SELL_ACTION_TYPES,
            submitted_qty=decision.qty,
            filled_qty=result.qty,
            remaining_qty=max(decision.qty - result.qty, 0.0),
            avg_fill_px=result.fill_price if result.qty > 0 else None,
            limit_px=result.fill_price,
            realized_pnl=result.realized_pnl,
            commission=result.commission,
            raw_status_sanitized=result.order_status or status.value,
            state_before=state.state.value,
            intended_state_after=decision.new_state.value if decision.new_state is not None else None,
            applied_state_after=state.state.value,
            created_at=result.timestamp,
        )
    raise TypeError(f"Unsupported dispatch result type: {type(result).__name__}")


def _sync_observation_state(target: ThesisState, source: ThesisState, *, allow_state_sync: bool) -> None:
    target.daily_pnl = source.daily_pnl
    target.current_notional = source.current_notional
    target.unrealized_pnl = source.unrealized_pnl
    target.thesis_pnl = source.thesis_pnl
    target.days_to_event = source.days_to_event
    target.highest_price_since_entry = source.highest_price_since_entry
    target.peak_unrealized_pnl_pct = source.peak_unrealized_pnl_pct
    target.initial_stop_price = source.initial_stop_price
    target.trailing_stop_price = source.trailing_stop_price
    target.protect_profit_mode = source.protect_profit_mode
    target.last_updated = source.last_updated
    target.liquidation_price = source.liquidation_price
    target.margin_used_usd = source.margin_used_usd
    target.leverage_used = source.leverage_used
    if allow_state_sync:
        target.state = source.state
        target.prior_state = source.prior_state


def _sync_post_fill_state(target: ThesisState, source: ThesisState) -> None:
    _sync_observation_state(target, source, allow_state_sync=False)
    target.prior_state = source.prior_state
    target.protect_profit_mode = source.protect_profit_mode
    target.runner_mode_active = source.runner_mode_active
    target.runner_target_qty = source.runner_target_qty
    target.tp_levels_triggered = list(source.tp_levels_triggered)
    target.target_price_tp_triggered = source.target_price_tp_triggered


def _log_cycle_safely(
    *,
    config: BotConfig,
    state: ThesisState,
    decision: Decision,
    fill,
    indicators,
    equity: float,
    state_before: str,
    order_result: Optional[OrderResult] = None,
    reconciliation_result=None,
    order_status_override: Optional[str] = None,
    order_reason_override: Optional[str] = None,
    order_qty_override: Optional[float] = None,
    risk_status_override: Optional[str] = None,
) -> None:
    try:
        run_dir = config.logging.get("run_dir", "reports/btc_hl_testnet")
        cfg_hash = config_snapshot_hash(config)
        log_cycle(
            run_dir=run_dir,
            state=state,
            decision=decision,
            fill=fill,
            indicators=indicators,
            equity=equity,
            state_before=state_before,
            state_after=state.state.value,
            config_hash=cfg_hash,
            order_result=order_result,
            reconciliation_result=reconciliation_result,
            order_status_override=order_status_override,
            order_reason_override=order_reason_override,
            order_qty_override=order_qty_override,
            risk_status_override=risk_status_override,
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("log_cycle failed: %s", exc)


# ── Main decision cycle (4h candle close) ─────────────────────────────────────

def run_decision_cycle(
    config: BotConfig,
    state: ThesisState,
    state_path: Path,
    client: HyperliquidClient,
    wallet_address: str,
    private_key: str,
    feed: Optional[HLWebSocketFeed] = None,
    dry_run: bool = False,
) -> str:
    """
    One full decision cycle (Section 8.1 steps a–i):
      a. Fetch latest candles via fetch_ohlcv_hl
      b. calc_indicators on candle history
      c. Fetch HL account snapshot
      d. engine_run (which itself reconciles in Step 3b)
      e. If decision.action != NO_ACTION and not dry_run: dispatch broker,
         apply_fill, write_state
      f. log_cycle
    Returns an explicit terminal status so the caller can stop scheduling
    after HALT or execution failure.
    """
    hl_cfg   = config.hl or {}
    coin     = hl_cfg.get("coin", "BTC")
    interval = hl_cfg.get("bar_interval", "4h")

    # a. Fetch candle history
    history_bars = int(config.data.get("required_history_days", 120))
    interval_hours = {"1m": 1/60, "5m": 5/60, "15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}.get(interval, 4.0)
    lookback_ms = int(history_bars * interval_hours * 3600 * 1000)
    now_ms = int(time.time() * 1000)
    df = fetch_ohlcv_hl(coin, interval, now_ms - lookback_ms, now_ms, client)

    # b. Compute indicators on the latest bar
    indicators = calc_indicators(df, config)

    # c. Fetch HL account snapshot for pre-decision reconciliation.
    # Pass the configured collateral_mode so spot USDC counts under unified
    # collateral but is ignored under clearinghouse_only.
    hl_snapshot = None
    collateral_mode = hl_cfg.get("collateral_mode", "unified")
    try:
        hl_snapshot = get_account_snapshot(
            wallet_address,
            client,
            collateral_mode=collateral_mode,
            include_open_orders=True,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("Could not fetch HL account snapshot: %s", exc)

    equity = float(config.capital.get("starting_equity", 0))
    if hl_snapshot is not None and hl_snapshot.account_value > 0:
        equity = hl_snapshot.account_value
    state_before = state.state.value

    if hl_snapshot is not None:
        try:
            recent_fills = get_recent_user_fills(wallet_address, client)
        except Exception as exc:  # noqa: BLE001
            _log.warning("Could not fetch recent HL fills: %s", exc)
            recent_fills = []

        state, reconcile_result = reconcile_state_with_exchange(
            state, hl_snapshot, recent_fills, config
        )
        stale_pending = mark_stale_pending_order_if_needed(state, config)
        if reconcile_result.status.value != "ok":
            write_state(state, str(state_path))
            decision = Decision(
                action=(
                    ActionType.HALT
                    if reconcile_result.status.value == "halted"
                    else ActionType.RECONCILE_POSITION
                ),
                qty=0.0,
                reason=reconcile_result.reason,
                blockers=[reconcile_result.halt_reason] if reconcile_result.halt_reason else [],
                new_state=BotState.HALTED if reconcile_result.status.value == "halted" else state.state,
            )
            risk_status = (
                "halted"
                if reconcile_result.status.value == "halted"
                else ("pending_order_open" if state.pending_order is not None else "reconciled")
            )
            _log_cycle_safely(
                config=config,
                state=state,
                decision=decision,
                fill=None,
                indicators=indicators,
                equity=equity,
                state_before=state_before,
                reconciliation_result=reconcile_result,
                order_status_override="no_order",
                order_reason_override=(
                    f"{reconcile_result.status.value}:{reconcile_result.reason}:"
                    f"exchange_qty={reconcile_result.exchange_position_qty}:"
                    f"local_qty={reconcile_result.local_position_qty}:"
                    f"open_orders={reconcile_result.open_orders_count}:"
                    f"matched_fills={reconcile_result.matched_fill_count}"
                ),
                order_qty_override=0.0,
                risk_status_override=risk_status,
            )
            if stale_pending:
                _STOP_EVENT.set()
                return STALE_PENDING_ORDER
            if reconcile_result.status.value == "halted":
                _STOP_EVENT.set()
                return HALTED
            if reconcile_result.status.value == "refreshed_pending_order":
                return BLOCKED_EXISTING_OPEN_ORDER
            return RECONCILED
        if stale_pending:
            write_state(state, str(state_path))
            _STOP_EVENT.set()
            return STALE_PENDING_ORDER

    # d. Run the decision engine on a working copy so intended state/mode
    # changes are not treated as applied position state before a fill exists.
    state_for_engine = state.model_copy(deep=True)
    decision = engine_run(state_for_engine, indicators, config, hl_snapshot=hl_snapshot)
    fill = None
    order_result = None
    _sync_observation_state(
        state,
        state_for_engine,
        allow_state_sync=(decision.action == ActionType.NO_ACTION),
    )

    # e. Execute, apply, persist
    if decision.action != ActionType.NO_ACTION and decision.action != ActionType.HALT:
        # Pre-order collateral gate: only relevant for HL networks + buys.
        # Sells / reduce-only do not consume new collateral, so we never block them.
        mode = config.bot.get("mode", "paper")
        is_buy = decision.action in _BUY_ACTION_TYPES
        if (mode in ("testnet", "mainnet")
                and is_buy
                and not dry_run
                and hl_snapshot is not None
                and not hl_snapshot.trading_collateral_available):
            blocker_reason = (
                f"insufficient_trading_collateral:{hl_snapshot.collateral_reason or 'unknown'}"
            )
            _log.error(
                "Blocking %s before order: effective_trading_collateral=%.4f "
                "trading_collateral_available=%s collateral_mode=%s",
                decision.action.value,
                hl_snapshot.effective_trading_collateral,
                hl_snapshot.trading_collateral_available,
                hl_snapshot.collateral_mode,
            )
            if blocker_reason not in decision.blockers:
                decision.blockers.append(blocker_reason)
            try:
                write_state(state, str(state_path))
            except Exception as exc:  # noqa: BLE001
                _log.error("write_state failed: %s", exc)
            _log_cycle_safely(
                config=config,
                state=state,
                decision=decision,
                fill=None,
                indicators=indicators,
                equity=equity,
                state_before=state_before,
                order_status_override="no_order",
                order_reason_override=blocker_reason,
                order_qty_override=0.0,
                risk_status_override="blocked",
            )
            _STOP_EVENT.set()
            return BLOCKED_NO_COLLATERAL

        if dry_run:
            _log.info("DRY RUN: would execute %s (qty=%s reason=%s)",
                      decision.action.value, decision.qty, decision.reason)
        else:
            cycle_risk_status_override = None
            mark_price = None
            if feed is not None:
                mark_price = feed.get_latest_price()
            if mark_price is None:
                mark_price = indicators.close or indicators.adj_close

            try:
                raw_order_result = _dispatch_broker(
                    decision, state, config, client,
                    wallet_address, private_key, mark_price,
                )
                order_result = _normalize_dispatch_result(raw_order_result, decision, state)
            except HLExecutionError as exc:
                order_result = OrderResult(
                    status=OrderResultStatus.EXECUTION_ERROR,
                    action=decision.action,
                    side="buy" if is_buy else "sell",
                    reduce_only=not is_buy,
                    submitted_qty=decision.qty,
                    filled_qty=0.0,
                    remaining_qty=decision.qty,
                    raw_status_sanitized="execution_error",
                    exchange_error_sanitized=str(exc)[:160],
                    state_before=state_before,
                    intended_state_after=decision.new_state.value if decision.new_state is not None else None,
                    applied_state_after=state.state.value,
                    created_at=datetime.now(timezone.utc),
                )
            action_class = classify_action(decision.action)

            if order_result.status == OrderResultStatus.EXECUTION_ERROR:
                state.state = BotState.HALTED
                state.halted = True
                state.halt_reason = (
                    f"hl_execution_error:{(order_result.exchange_error_sanitized or 'execution_error')[:160]}"
                )
                write_state(state, str(state_path))
                _log_cycle_safely(
                    config=config,
                    state=state,
                    decision=decision,
                    fill=None,
                    indicators=indicators,
                    equity=equity,
                    state_before=state_before,
                    order_result=order_result,
                    order_status_override="failed",
                    order_reason_override=state.halt_reason,
                    order_qty_override=decision.qty,
                    risk_status_override="halted",
                )
                _STOP_EVENT.set()
                return EXECUTION_ERROR
            if order_result.status == OrderResultStatus.UNKNOWN:
                state.state = BotState.HALTED
                state.halted = True
                state.halt_reason = "unknown_order_result"
                write_state(state, str(state_path))
                _log_cycle_safely(
                    config=config,
                    state=state,
                    decision=decision,
                    fill=None,
                    indicators=indicators,
                    equity=equity,
                    state_before=state_before,
                    order_result=order_result,
                    risk_status_override="halted",
                )
                _STOP_EVENT.set()
                return HALTED
            if order_result.status == OrderResultStatus.REJECTED:
                if action_class in {ActionClass.RISK_REDUCING, ActionClass.EMERGENCY_EXIT}:
                    state.state = BotState.HALTED
                    state.halted = True
                    state.halt_reason = (
                        f"risk_reducing_order_rejected:{order_result.exchange_error_sanitized or 'rejected'}"
                    )
                    order_result.applied_state_after = state.state.value
                    write_state(state, str(state_path))
                    _log_cycle_safely(
                        config=config,
                        state=state,
                        decision=decision,
                        fill=None,
                        indicators=indicators,
                        equity=equity,
                        state_before=state_before,
                        order_result=order_result,
                        risk_status_override="halted",
                    )
                    _STOP_EVENT.set()
                    return HALTED
                write_state(state, str(state_path))
                _log_cycle_safely(
                    config=config,
                    state=state,
                    decision=decision,
                    fill=None,
                    indicators=indicators,
                    equity=equity,
                    state_before=state_before,
                    order_result=order_result,
                    risk_status_override="blocked",
                )
                return CONTINUE

            if is_unfilled_emergency_exit(order_result):
                is_ioc_exit = _is_ioc_execution(config)
                state.pending_order = (
                    None if is_ioc_exit and decision.action != ActionType.SELL_STOP
                    else _build_pending_order(state, decision, order_result)
                )
                if decision.action == ActionType.SELL_STOP:
                    state.state = BotState(state_before)
                    state.halted = False
                    state.halt_reason = None
                else:
                    state.state = BotState.HALTED
                    state.halted = True
                    state.halt_reason = "ioc_emergency_exit_unfilled" if is_ioc_exit else "unfilled_emergency_exit"
                order_result.applied_state_after = state.state.value
                write_state(state, str(state_path))
                _log_cycle_safely(
                    config=config,
                    state=state,
                    decision=decision,
                    fill=None,
                    indicators=indicators,
                    equity=equity,
                    state_before=state_before,
                    order_result=order_result,
                    risk_status_override=(
                        "pending_exit" if decision.action == ActionType.SELL_STOP else "halted"
                    ),
                )
                if decision.action == ActionType.SELL_STOP:
                    return BLOCKED_EXISTING_OPEN_ORDER
                _STOP_EVENT.set()
                return HALTED

            if order_result.filled_qty > 0:
                fill = _order_result_to_fill(order_result)
                if fill is not None:
                    state = apply_fill(state, fill)
                apply_intended_state = False
                apply_decision_side_effects = False
                if decision.action in _BUY_ACTION_TYPES:
                    apply_intended_state = True
                    apply_decision_side_effects = True
                elif decision.action in _SELL_ACTION_TYPES and order_result.remaining_qty <= 0.0:
                    apply_intended_state = True
                    apply_decision_side_effects = True
                    if decision.new_state == BotState.RUNNER_LONG:
                        runner_target = state_for_engine.runner_target_qty
                        if runner_target is None or state.current_position_qty > runner_target + 1e-9:
                            apply_intended_state = False
                            apply_decision_side_effects = False
                    if decision.new_state == BotState.EXITED and state.current_position_qty > 1e-9:
                        apply_intended_state = False
                        apply_decision_side_effects = False
                if apply_decision_side_effects:
                    _sync_post_fill_state(state, state_for_engine)
                if apply_intended_state and decision.new_state is not None:
                    state.state = decision.new_state
                    if state.state == BotState.HALTED:
                        state.halted = True
                        state.halt_reason = state.halt_reason or decision.reason
                if order_result.remaining_qty > 0.0:
                    order_result.applied_state_after = state.state.value
                    state.pending_order = _build_pending_order(state, decision, order_result)
                    cycle_risk_status_override = (
                        "pending_exit" if action_class == ActionClass.EMERGENCY_EXIT else "pending_reduce"
                    )
                else:
                    state.pending_order = None
                    order_result.applied_state_after = state.state.value
                if is_residual_emergency_exit(order_result) or (
                    action_class == ActionClass.EMERGENCY_EXIT
                    and state.current_position_qty > 1e-9
                ):
                    is_ioc_exit = _is_ioc_execution(config)
                    if decision.action == ActionType.SELL_STOP:
                        state.state = BotState(state_before)
                        state.halted = False
                        state.halt_reason = None
                    else:
                        if is_ioc_exit:
                            state.pending_order = None
                        state.state = BotState.HALTED
                        state.halted = True
                        state.halt_reason = (
                            "ioc_emergency_exit_partial_residual"
                            if is_ioc_exit
                            else "residual_position_after_emergency_exit"
                        )
                    order_result.applied_state_after = state.state.value
                    write_state(state, str(state_path))
                    _log_cycle_safely(
                        config=config,
                        state=state,
                        decision=decision,
                        fill=fill,
                        indicators=indicators,
                        equity=equity,
                        state_before=state_before,
                        order_result=order_result,
                        risk_status_override=(
                            "pending_exit" if decision.action == ActionType.SELL_STOP else "halted"
                        ),
                    )
                    if decision.action == ActionType.SELL_STOP:
                        return BLOCKED_EXISTING_OPEN_ORDER
                    _STOP_EVENT.set()
                    return HALTED
            elif order_result.status == OrderResultStatus.SUBMITTED_UNFILLED:
                order_result.applied_state_after = state.state.value
                state.pending_order = _build_pending_order(state, decision, order_result)
                cycle_risk_status_override = (
                    "pending_event_risk_reduce"
                    if decision.action == ActionType.SELL_EVENT_DERISKING
                    else (
                        "pending_reduce"
                        if action_class == ActionClass.RISK_REDUCING
                        else ("pending_order_open" if action_class in {ActionClass.OPENING, ActionClass.ADDING} else None)
                    )
                )
    elif decision.action == ActionType.HALT:
        state.state = decision.new_state or BotState.HALTED
        state.halted = True
        state.halt_reason = state.halt_reason or decision.reason
        write_state(state, str(state_path))
        _log_cycle_safely(
            config=config,
            state=state,
            decision=decision,
            fill=None,
            indicators=indicators,
            equity=equity,
            state_before=state_before,
            risk_status_override="halted",
        )
        _STOP_EVENT.set()
        return HALTED

    # f. Optional: apply funding for the closed bar (preserved from user edit)
    mark_price_for_funding = None
    if feed is not None:
        mark_price_for_funding = feed.get_latest_price()
    if mark_price_for_funding is None:
        mark_price_for_funding = indicators.close or indicators.adj_close
    state = apply_candle_close_funding(state, coin, client, mark_price_for_funding, hours=interval_hours)

    # g. Persist state — never skip
    try:
        write_state(state, str(state_path))
    except Exception as exc:  # noqa: BLE001
        _log.error("write_state failed: %s", exc)

    # h. Log to CSVs (orders, positions, risk, decisions)
    _log_cycle_safely(
        config=config,
        state=state,
        decision=decision,
        fill=fill,
        indicators=indicators,
        equity=equity,
        state_before=state_before,
        order_result=order_result,
        risk_status_override=cycle_risk_status_override if 'cycle_risk_status_override' in locals() else None,
    )

    # i. Generate and log the daily summary for operator visibility.
    try:
        summary = format_summary(
            state=state,
            indicators=indicators,
            config=config,
            action_label=decision.action.value.upper(),
            reason=decision.reason,
            blockers=decision.blockers,
        )
        _log.info("Daily summary:\n%s", summary)
    except Exception as exc:  # noqa: BLE001
        _log.error("daily summary generation failed: %s", exc)

    return STOPPED if _STOP_EVENT.is_set() else CONTINUE


# ── Intraday monitor (stop checks on WS price) ────────────────────────────────

def run_intraday_monitor(
    feed: HLWebSocketFeed,
    stop_event: threading.Event,
    *,
    state: Optional[ThesisState] = None,
    state_path: Optional[Path] = None,
    config: Optional[BotConfig] = None,
    client: Optional[HyperliquidClient] = None,
    wallet_address: Optional[str] = None,
    private_key: Optional[str] = None,
    check_interval_seconds: int = 60,
) -> None:
    """
    Background thread: polls WS price and evaluates stop triggers.

    When state/config/client/wallet/private_key are all provided, an intraday
    stop crossing triggers an immediate EXIT_ALL via the dispatched broker,
    state is persisted, and the global stop_event is set so the 4h loop
    shuts down cleanly. When called without these (e.g. from a test or a
    paper-only run), the monitor only logs prices.
    """
    _log.info("Intraday monitor started")
    while not stop_event.is_set():
        price = feed.get_latest_price()
        if price is None:
            stop_event.wait(timeout=check_interval_seconds)
            continue

        _log.debug("Intraday price: %.4f", price)

        if (state is not None
                and config is not None
                and state.current_position_qty > 0):
            trigger_reason: Optional[str] = None
            if (state.trailing_stop_price is not None
                    and price <= state.trailing_stop_price):
                trigger_reason = "intraday_trailing_stop_triggered"
            elif (state.initial_stop_price is not None
                    and price <= state.initial_stop_price):
                trigger_reason = "intraday_hard_stop_triggered"

            if trigger_reason is not None and client is not None and wallet_address is not None and private_key is not None:
                _log.warning("Intraday stop %s @ price=%.4f — exiting", trigger_reason, price)
                exit_decision = Decision(
                    action=ActionType.EXIT_ALL,
                    qty=state.current_position_qty,
                    reason=trigger_reason,
                    new_state=BotState.EXITED,
                )
                try:
                    raw_result = _dispatch_broker(
                        exit_decision, state, config, client,
                        wallet_address, private_key, price,
                    )
                    order_result = _normalize_dispatch_result(raw_result, exit_decision, state)
                    fill = _order_result_to_fill(order_result)
                    if fill is not None:
                        apply_fill(state, fill)
                    if order_result.filled_qty > 0 and state.current_position_qty <= 1e-9:
                        state.state = BotState.EXITED
                    else:
                        state.state = BotState.HALTED
                        state.halted = True
                        state.halt_reason = "intraday_exit_not_fully_filled"
                    if state_path is not None:
                        write_state(state, str(state_path))
                except Exception as exc:  # noqa: BLE001
                    _log.error("Intraday exit execution failed: %s", exc)
                    state.state = BotState.HALTED
                    state.halted = True
                    state.halt_reason = f"intraday_exit_failed: {exc}"
                    if state_path is not None:
                        write_state(state, str(state_path))
                # Whether or not execution succeeded, stop the loop so an
                # operator can investigate. A failed exit must not retry blindly.
                stop_event.set()
                break

        stop_event.wait(timeout=check_interval_seconds)
    _log.info("Intraday monitor stopped")


# ── Main entry ────────────────────────────────────────────────────────────────

def main(
    config_path: str = "config/btc_long_thesis.yaml",
    mainnet: bool = False,
    dry_run: bool = False,
    one_cycle: bool = False,
) -> None:
    _log.info("Loading config from %s", config_path)
    config = load_config(config_path)

    if mainnet:
        try:
            validate_mainnet_intent(config, cli_has_mainnet_flag=True)
        except ValueError as exc:
            raise SystemExit(f"ERROR: {exc}") from exc
    elif config.bot.get("mode") == "mainnet":
        raise SystemExit(
            "ERROR: config has bot.mode=mainnet but --mainnet flag was not passed. "
            "Refusing to start."
        )

    private_key = startup_checks(config)
    _install_shutdown_handler(_STOP_EVENT)

    hl_cfg = config.hl or {}
    coin           = hl_cfg.get("coin", "BTC")
    network        = hl_cfg.get("network", "testnet")
    wallet_address = hl_cfg.get("wallet_address", "")
    ws_url         = MAINNET_WS_URL if network == "mainnet" else TESTNET_WS_URL

    if config.bot.get("mode") == "mainnet":
        max_risk = (
            float(config.capital["starting_equity"])
            * float(config.capital["max_total_capital_at_risk_pct"])
        )
        _log.warning("WARNING: MAINNET MODE ACTIVE. Real funds at risk.")
        _log.warning("Coin: %s | Max risk: $%.2f", coin, max_risk)
        time.sleep(5)

    _log.info("Startup checks passed — entering live loop (%s)", network)
    client = HyperliquidClient(network=network)

    # Load persisted state if it exists, otherwise start fresh. If an existing
    # state file cannot be trusted, persist HALTED and abort before WS/network
    # polling or scheduling can begin.
    run_dir = Path(config.logging.get("run_dir", "reports/btc_hl_testnet"))
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state, terminal_state_load = load_state_or_halt(
        str(state_path), config.symbol.get("ticker", coin), logger=_log
    )
    if terminal_state_load:
        _STOP_EVENT.set()
        _log.critical("State load is terminal (%s); refusing to start live loop",
                      state.halt_reason)
        return
    _log.info("Loaded state (state=%s, qty=%.6f)",
              state.state.value, state.current_position_qty)

    if config.hl and state.sz_decimals == 0:
        state.sz_decimals = int(config.hl.get("sz_decimals", 0) or 0)

    if one_cycle:
        _log.info("Running one-cycle decision probe")
        try:
            status = run_decision_cycle(
                config, state, state_path, client,
                wallet_address, private_key, feed=None, dry_run=dry_run,
            )
            _log.info("One-cycle decision finished with status %s", status)
        finally:
            _STOP_EVENT.set()
        return

    # Start WS feed + intraday monitor with full state-aware stop checks
    feed = HLWebSocketFeed(ws_url=ws_url, coin=coin)
    feed.start()
    _log.info("WebSocket feed started for %s @ %s", coin, ws_url)

    monitor_thread = threading.Thread(
        target=run_intraday_monitor,
        args=(feed, _STOP_EVENT),
        kwargs={
            "state":          state,
            "state_path":     state_path,
            "config":         config,
            "client":         client,
            "wallet_address": wallet_address,
            "private_key":    private_key,
        },
        daemon=True,
        name="intraday-monitor",
    )
    monitor_thread.start()

    try:
        while not _STOP_EVENT.is_set():
            target = next_4h_candle_close()
            wait_secs = (target - datetime.now(timezone.utc)).total_seconds()
            _log.info("Next 4h candle close at %s (%.0fs away)", target.isoformat(), wait_secs)

            deadline = time.monotonic() + wait_secs
            while time.monotonic() < deadline and not _STOP_EVENT.is_set():
                time.sleep(min(5.0, deadline - time.monotonic()))

            if _STOP_EVENT.is_set():
                break

            _log.info("4h candle closed at %s — running decision engine", target.isoformat())
            try:
                status = run_decision_cycle(
                    config, state, state_path, client,
                    wallet_address, private_key, feed=feed, dry_run=dry_run,
                )
                if status != CONTINUE:
                    _log.warning("Decision cycle returned terminal status %s; stopping", status)
                    _STOP_EVENT.set()
                    break
            except Exception as exc:  # noqa: BLE001
                _log.error("Decision cycle failed: %s", exc)
                _STOP_EVENT.set()
                break
    finally:
        _log.info("Shutting down — stopping WS feed")
        feed.stop()
        monitor_thread.join(timeout=10)
        _log.info("Shutdown complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HL live loop")
    parser.add_argument("config_path", nargs="?", help="Backward-compatible positional config path")
    parser.add_argument(
        "--config",
        default="config/btc_long_thesis.yaml",
        help="Path to bot config YAML.",
    )
    parser.add_argument("--mainnet", action="store_true",
                        help="Required to run against mainnet (must also pass 4-layer gate)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the decision loop but skip order placement")
    parser.add_argument("--one-cycle", action="store_true",
                        help="Run exactly one decision cycle now and exit without starting the recurring 4h loop")
    args = parser.parse_args()
    main(
        args.config_path or args.config,
        mainnet=args.mainnet,
        dry_run=args.dry_run,
        one_cycle=args.one_cycle,
    )
