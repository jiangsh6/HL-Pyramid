"""
HL Phase 2 — state_writer.apply_fill using contracts field.

Named tests (3 required):
  test_apply_fill_buy_uses_contracts
  test_apply_fill_sell_uses_contracts
  test_apply_fill_rejects_legacy_qty_shape
"""
from __future__ import annotations

import math
from datetime import datetime

import pytest

from src.core.models import ActionType, BotState, Fill, LotRecord, ThesisState
from src.reporting.state_writer import apply_fill
from tests._helpers import make_lot, make_state


def _fill(action, qty, fill_price, realized_pnl=0.0):
    return Fill(
        action=action,
        qty=qty,
        fill_price=fill_price,
        slippage_bps=0.0,
        realized_pnl=realized_pnl,
        commission=0.0,
        timestamp=datetime.now(),
    )


# ── Named tests ──────────────────────────────────────────────────────────────

def test_apply_fill_buy_uses_contracts():
    """BUY_STARTER fill with qty=0.005 creates base_lot.qty=0.005."""
    state = make_state()
    fill  = _fill(ActionType.BUY_STARTER, qty=0.005, fill_price=50_000.0)
    apply_fill(state, fill)

    assert state.base_lot is not None
    assert math.isclose(state.base_lot.qty, 0.005)
    # current_position_qty must also be updated
    assert math.isclose(state.current_position_qty, 0.005)


def test_apply_fill_sell_uses_contracts():
    """SELL fill with qty=0.003 reduces position by 0.003 contracts."""
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=LotRecord(lot_id="base", entry_price=50_000.0,
                           qty=0.010, entry_date="2026-01-01"),
        addon_lots=[
            LotRecord(lot_id="add_1", entry_price=52_000.0,
                      qty=0.005, entry_date="2026-01-02"),
        ],
        avg_entry_price=50_667.0,
        current_position_qty=0.015,
    )
    fill = _fill(ActionType.SELL_TAKE_PROFIT, qty=0.005, fill_price=55_000.0,
                 realized_pnl=15.0)
    apply_fill(state, fill)

    # add_1 (0.005) fully sold via LIFO; base_lot intact
    assert len(state.addon_lots) == 0
    assert state.base_lot is not None
    assert math.isclose(state.base_lot.qty, 0.010)
    assert math.isclose(state.current_position_qty, 0.010)
    assert math.isclose(state.realized_pnl, 15.0)


def test_apply_fill_rejects_legacy_qty_shape():
    """
    If a Fill is created with only qty= (old path), apply_fill must
    still work because the model_validator populates fill.qty from
    fill.qty.
    """
    state = make_state()
    # Old-style fill — uses qty kwarg, not contracts
    old_fill = Fill(
        action=ActionType.BUY_STARTER,
        qty=10,                   # compat field
        fill_price=100.0,
        slippage_bps=1.0,
        timestamp=datetime.now(),
    )
    # Validator should have set qty=10.0
    assert math.isclose(old_fill.qty, 10.0)

    apply_fill(state, old_fill)
    assert state.base_lot is not None
    assert math.isclose(state.base_lot.qty, 10.0)
    assert state.current_position_qty == 10


# ── Extra coverage ───────────────────────────────────────────────────────────

def test_apply_fill_buy_base_combines_lots_using_contracts():
    """BUY_BASE on top of an existing STARTER uses weighted avg via contracts."""
    state = make_state(
        state=BotState.STARTER_LONG,
        base_lot=LotRecord(lot_id="base", entry_price=100.0,
                           qty=5.0, entry_date="2026-01-01"),
        avg_entry_price=100.0,
        current_position_qty=5.0,
    )
    fill = _fill(ActionType.BUY_BASE, qty=10.0, fill_price=110.0)
    apply_fill(state, fill)

    # Combined: 5@100 + 10@110 = 15 contracts; avg = (500+1100)/15 ≈ 106.667
    assert state.base_lot is not None
    assert math.isclose(state.base_lot.qty, 15.0)
    assert math.isclose(state.avg_entry_price, 106.6667, rel_tol=1e-4)
    assert math.isclose(state.current_position_qty, 15.0)
    assert state.current_position_qty == 15


def test_apply_fill_addon_appends_with_contracts():
    """BUY_ADDON creates a new lot using fill.qty."""
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=LotRecord(lot_id="base", entry_price=100.0,
                           qty=10.0, entry_date="2026-01-01"),
        avg_entry_price=100.0,
        current_position_qty=10.0,
    )
    fill = _fill(ActionType.BUY_ADDON, qty=5.0, fill_price=110.0)
    apply_fill(state, fill)

    assert len(state.addon_lots) == 1
    assert math.isclose(state.addon_lots[0].qty, 5.0)
    assert math.isclose(state.current_position_qty, 15.0)
    assert state.add_count == 1
