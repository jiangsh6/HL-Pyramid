"""
state.json read/write and apply_fill.  Section 7.

state.json is the SINGLE SOURCE OF TRUTH in paper mode (Section 7.3).
paper_broker is stateless; this module applies Fills to ThesisState.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from src.core.models import (
    ActionType, BotState, Fill, LotRecord, ThesisState,
)
from src.strategy.reduce import lifo_reduce, recalc_avg_entry_price


_BUY_ACTIONS = {ActionType.BUY_STARTER, ActionType.BUY_BASE, ActionType.BUY_ADDON}
_SELL_ACTIONS = {
    ActionType.SELL_REDUCE_ADDON, ActionType.SELL_REDUCE_BASE,
    ActionType.SELL_TAKE_PROFIT, ActionType.SELL_STOP,
    ActionType.SELL_TRAILING_STOP, ActionType.SELL_EVENT_DERISKING,
    ActionType.EXIT_ALL,
}


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
    """
    if fill.shares <= 0:
        return state
    if fill.action not in (_BUY_ACTIONS | _SELL_ACTIONS):
        return state

    today = _fill_date(fill)

    if fill.action == ActionType.BUY_STARTER:
        state.base_lot = LotRecord(
            lot_id="base", entry_price=fill.fill_price,
            shares=fill.shares, entry_date=today,
        )
        state.entry_date = today
        state.add_count = 0
        state.last_add_price = fill.fill_price

    elif fill.action == ActionType.BUY_BASE:
        if state.base_lot is None:
            state.base_lot = LotRecord(
                lot_id="base", entry_price=fill.fill_price,
                shares=fill.shares, entry_date=today,
            )
            if state.entry_date is None:
                state.entry_date = today
        else:
            # Combine starter + base into a single base_lot with weighted avg
            existing = state.base_lot.shares
            total = existing + fill.shares
            new_avg = (
                existing * state.base_lot.entry_price + fill.shares * fill.fill_price
            ) / total
            state.base_lot = LotRecord(
                lot_id="base", entry_price=new_avg,
                shares=total, entry_date=state.base_lot.entry_date,
            )
        recalc_avg_entry_price(state)
        state.last_add_price = state.avg_entry_price

    elif fill.action == ActionType.BUY_ADDON:
        new_id = f"add_{state.add_count + 1}"
        state.addon_lots = list(state.addon_lots) + [
            LotRecord(
                lot_id=new_id, entry_price=fill.fill_price,
                shares=fill.shares, entry_date=today,
            )
        ]
        state.add_count += 1
        state.last_add_price = fill.fill_price

    elif fill.action in _SELL_ACTIONS:
        remaining = fill.shares
        if state.addon_lots:
            updated, sold = lifo_reduce(state.addon_lots, remaining)
            state.addon_lots = updated
            remaining -= sold
        if remaining > 0 and state.base_lot is not None:
            sell_from_base = min(state.base_lot.shares, remaining)
            new_shares = state.base_lot.shares - sell_from_base
            if new_shares > 0:
                state.base_lot = LotRecord(
                    lot_id=state.base_lot.lot_id,
                    entry_price=state.base_lot.entry_price,
                    shares=new_shares,
                    entry_date=state.base_lot.entry_date,
                )
            else:
                state.base_lot = None
        state.realized_pnl += fill.realized_pnl

    recalc_avg_entry_price(state)
    if state.current_position_shares > 0 and state.avg_entry_price:
        state.current_notional = state.current_position_shares * state.avg_entry_price
    else:
        state.current_notional = 0.0

    return state


def write_state(state: ThesisState, path: str) -> None:
    """Serialize state to JSON.  Raises on failure per Section 7.3."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = state.model_dump(mode="json")
    p.write_text(json.dumps(data, indent=2, default=str))


def read_state(path: str) -> ThesisState:
    """Load state from JSON.  Pydantic validates the schema."""
    raw = json.loads(Path(path).read_text())
    return ThesisState(**raw)


def reset_state_to_flat(state: ThesisState) -> ThesisState:
    """Manual reset (HALTED/EXITED → FLAT).  Used by reset_state.py."""
    state.state = BotState.FLAT
    state.prior_state = None
    state.protect_profit_mode = False
    state.runner_mode_active = False
    state.runner_target_shares = None
    state.base_lot = None
    state.addon_lots = []
    state.avg_entry_price = None
    state.current_position_shares = 0
    state.current_notional = 0.0
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
    return state
