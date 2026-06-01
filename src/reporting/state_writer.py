"""
state.json read/write and apply_fill.  Section 7.

state.json is the SINGLE SOURCE OF TRUTH in paper mode (Section 7.3).
paper_broker is stateless; this module applies Fills to ThesisState.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Tuple

from pydantic import ValidationError

from src.core.models import (
    EPSILON, ActionType, BotState, Fill, LotRecord, ThesisState,
)
from src.strategy.reduce import lifo_reduce, recalc_avg_entry_price


_BUY_ACTIONS = {ActionType.BUY_STARTER, ActionType.BUY_BASE, ActionType.BUY_ADDON}
_SELL_ACTIONS = {
    ActionType.SELL_REDUCE_ADDON, ActionType.SELL_REDUCE_BASE,
    ActionType.SELL_TAKE_PROFIT, ActionType.SELL_STOP,
    ActionType.SELL_TRAILING_STOP, ActionType.SELL_EVENT_DERISKING,
    ActionType.EXIT_ALL,
}


def _dust_policy(config) -> dict:
    hl_cfg = getattr(config, "hl", None) or {}
    policy = hl_cfg.get("dust_policy") or {}
    return {
        "enabled": bool(policy.get("enabled", True)),
        "dust_qty_threshold": float(policy.get("dust_qty_threshold", hl_cfg.get("min_size", 0.0) or 0.0)),
        "dust_notional_threshold_usd": float(policy.get("dust_notional_threshold_usd", 10.0)),
        "action": policy.get("action", "halt_and_operator_review"),
        "allow_reduce_only_dust_close": bool(policy.get("allow_reduce_only_dust_close", False)),
        "allow_round_up_to_min_size_for_reduce_only": bool(
            policy.get("allow_round_up_to_min_size_for_reduce_only", False)
        ),
    }


def update_dust_state(
    state: ThesisState,
    mark_price: float,
    config,
) -> bool:
    """
    Mark tiny non-zero HL perp residuals explicitly.

    A dust position is still exposure. Under the default policy the bot HALTs
    for operator review instead of treating it as flat or silently ignoring it.
    """
    policy = _dust_policy(config)
    if not policy["enabled"]:
        state.dust_position = False
        state.dust_qty = 0.0
        state.dust_notional = 0.0
        state.dust_reason = None
        return False

    qty = state.current_position_qty
    threshold_qty = policy["dust_qty_threshold"]
    notional = qty * mark_price if mark_price > 0 else 0.0
    is_dust = qty > EPSILON and threshold_qty > 0 and qty < threshold_qty
    if is_dust:
        state.dust_position = True
        state.dust_qty = qty
        state.dust_notional = notional
        state.dust_reason = "qty_below_min_size_dust_position"
        if policy["action"] == "halt_and_operator_review":
            state.state = BotState.HALTED
            state.halted = True
            state.halt_reason = "dust_position_below_min_size"
        return True

    state.dust_position = False
    state.dust_qty = 0.0
    state.dust_notional = 0.0
    state.dust_reason = None
    return False


def _fill_date(fill: Fill) -> date:
    ts = fill.timestamp
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    return date.fromisoformat(str(ts))


def apply_fill(state: ThesisState, fill: Fill) -> ThesisState:
    """
    Apply a Fill to state in place.  Returns the same state object for chaining.
    Recalculates avg_entry_price after every fill (Section 7.2).

    Uses fill.qty (float) as the only position quantity.
    """
    fill_qty = fill.qty

    if fill_qty <= EPSILON:
        return state
    if fill.action not in (_BUY_ACTIONS | _SELL_ACTIONS):
        return state

    today    = _fill_date(fill)
    sz_dec   = state.sz_decimals  # 0 for equity compat, 3 for BTC, etc.
    min_size = 0.0                # Phase 3 will pull from HLAssetMeta

    if fill.action == ActionType.BUY_STARTER:
        state.pending_order = None
        state.base_lot = LotRecord(
            lot_id="base", entry_price=fill.fill_price,
            qty=fill_qty, entry_date=today,
        )
        state.original_base_qty = fill_qty
        state.entry_date = today
        state.add_count = 0
        state.last_add_price = fill.fill_price

    elif fill.action == ActionType.BUY_BASE:
        state.pending_order = None
        if state.base_lot is None:
            state.base_lot = LotRecord(
                lot_id="base", entry_price=fill.fill_price,
                qty=fill_qty, entry_date=today,
            )
            state.original_base_qty = fill_qty
            if state.entry_date is None:
                state.entry_date = today
        else:
            # Combine starter + base into single base_lot with weighted avg price
            existing  = state.base_lot.qty
            total     = existing + fill_qty
            new_avg   = (
                existing * state.base_lot.entry_price + fill_qty * fill.fill_price
            ) / total
            state.base_lot = LotRecord(
                lot_id="base", entry_price=new_avg,
                qty=total, entry_date=state.base_lot.entry_date,
            )
            state.original_base_qty = total
        recalc_avg_entry_price(state)
        state.last_add_price = state.avg_entry_price

    elif fill.action == ActionType.BUY_ADDON:
        state.pending_order = None
        new_id = f"add_{state.add_count + 1}"
        state.addon_lots = list(state.addon_lots) + [
            LotRecord(
                lot_id=new_id, entry_price=fill.fill_price,
                qty=fill_qty, entry_date=today,
            )
        ]
        state.add_count += 1
        state.last_add_price = fill.fill_price

    elif fill.action in _SELL_ACTIONS:
        state.pending_order = None
        remaining = fill_qty
        if state.addon_lots:
            updated, sold = lifo_reduce(
                state.addon_lots, remaining,
                sz_decimals=sz_dec, min_size=min_size,
            )
            state.addon_lots = updated
            remaining -= sold
        if remaining > 0 and state.base_lot is not None:
            sell_from_base = min(state.base_lot.qty, remaining)
            new_qty = round(state.base_lot.qty - sell_from_base, sz_dec) if sz_dec > 0 else state.base_lot.qty - sell_from_base
            if new_qty > max(min_size, EPSILON):
                state.base_lot = LotRecord(
                    lot_id=state.base_lot.lot_id,
                    entry_price=state.base_lot.entry_price,
                    qty=new_qty,
                    entry_date=state.base_lot.entry_date,
                )
            else:
                state.base_lot = None
        state.realized_pnl += fill.realized_pnl
        state.daily_pnl    += fill.realized_pnl
        state.thesis_pnl   += fill.realized_pnl

    recalc_avg_entry_price(state)
    if state.current_position_qty > EPSILON and state.avg_entry_price:
        state.current_notional = state.current_position_qty * state.avg_entry_price
    else:
        state.current_notional = 0.0

    # Track when state was last touched so decision_engine can detect day rollovers.
    if isinstance(fill.timestamp, datetime):
        state.last_updated = fill.timestamp

    return state


def write_state(state: ThesisState, path: str) -> None:
    """Serialize state to JSON.  Raises on failure per Section 7.3."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = state.model_dump(mode="json")
    p.write_text(json.dumps(data, indent=2, default=str))


def halted_state(symbol: str, reason: str) -> ThesisState:
    """Construct a minimal valid HALTED state for startup-fatal state errors."""
    return ThesisState(
        symbol=symbol,
        state=BotState.HALTED,
        halted=True,
        halt_reason=reason,
    )


def read_state(path: str) -> ThesisState:
    """Load state from JSON.  Pydantic validates the schema."""
    raw = json.loads(Path(path).read_text())
    try:
        return ThesisState(**raw)
    except ValidationError as exc:
        legacy_fields = (
            "shares",
            "current_position_" + "shares",
            "base_position_" + "shares",
            "original_base_" + "shares",
            "runner_target_" + "shares",
        )
        if any(field in str(exc) for field in legacy_fields):
            return ThesisState(
                symbol=str(raw.get("symbol", "UNKNOWN")),
                state=BotState.HALTED,
                halted=True,
                halt_reason="legacy_share_state_not_supported",
            )
        raise


def load_state_or_halt(
    path: str,
    symbol: str,
    *,
    logger=None,
) -> Tuple[ThesisState, bool]:
    """
    Load state safely at startup.

    Returns (state, terminal). If an existing state file is unreadable,
    malformed, schema-invalid, or legacy share-shaped, a minimal HALTED
    state is persisted and terminal=True tells callers not to trade or
    start fresh over unknown exposure.
    """
    state_path = Path(path)
    if not state_path.exists():
        return ThesisState(symbol=symbol), False

    try:
        state = read_state(str(state_path))
    except Exception as exc:  # noqa: BLE001
        if logger is not None:
            logger.error("State load/validation failed: %s", exc)
        state = halted_state(symbol, "state_load_or_validation_error")
        write_state(state, str(state_path))
        return state, True

    if state.halted and state.halt_reason == "legacy_share_state_not_supported":
        write_state(state, str(state_path))
        return state, True
    return state, False


def mark_to_market_state(state: ThesisState, mark_price: float) -> ThesisState:
    """
    Refresh quantity, weighted entry, notional, unrealized PnL, and thesis
    PnL from the latest mark price for the long-only bot.
    """
    recalc_avg_entry_price(state)
    if state.current_position_qty > EPSILON and state.avg_entry_price is not None:
        state.current_notional = state.current_position_qty * mark_price
        state.unrealized_pnl = (
            state.current_position_qty * (mark_price - state.avg_entry_price)
        )
    else:
        state.current_notional = 0.0
        state.unrealized_pnl = 0.0
    state.thesis_pnl = (
        state.realized_pnl + state.unrealized_pnl + state.cumulative_funding_pnl
    )
    return state


def reset_state_to_flat(state: ThesisState) -> ThesisState:
    """Manual reset (HALTED/EXITED → FLAT).  Used by reset_state.py."""
    state.state = BotState.FLAT
    state.prior_state = None
    state.protect_profit_mode = False
    state.runner_mode_active = False
    state.runner_target_qty = None
    state.base_lot = None
    state.addon_lots = []
    state.avg_entry_price               = None
    state.current_position_qty    = 0.0
    state.original_base_qty       = 0.0
    state.current_notional              = 0.0
    state.add_count = 0
    state.last_add_price = None
    state.highest_price_since_entry = None
    state.peak_unrealized_pnl_pct = 0.0
    state.initial_stop_price = None
    state.trailing_stop_price = None
    state.tp_levels_triggered = [False, False, False, False]
    state.target_price_tp_triggered = False
    state.realized_pnl = 0.0
    state.unrealized_pnl = 0.0
    state.thesis_pnl = 0.0
    state.halted = False
    state.halt_reason = None
    state.entry_date = None
    state.last_action = None
    state.days_to_event = None
    # HL audit H3: reset all HL-specific fields so stale values from a
    # prior thesis cannot affect a fresh run (e.g. stale liquidation_price
    # triggering false risk-validator HALTs).
    state.cumulative_funding_pnl    = 0.0
    state.liquidation_price         = None
    state.margin_used_usd           = 0.0
    state.leverage_used             = 0.0
    state.dust_position             = False
    state.dust_qty                  = 0.0
    state.dust_notional             = 0.0
    state.dust_reason               = None
    state.last_funding_rate         = 0.0
    state.next_funding_timestamp    = None
    state.runner_target_qty             = None
    state.sz_decimals               = 0
    state.daily_pnl                 = 0.0
    state.pending_order             = None
    return state
