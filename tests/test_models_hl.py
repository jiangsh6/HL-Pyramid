"""
HL Phase 2 — Model and reduce tests for float contracts.

Named tests (8 required):
  test_lot_record_uses_contracts_field
  test_thesis_state_contracts_field_defaults_zero
  test_hl_asset_meta_validates_correctly
  test_fill_uses_contracts_field
  test_decision_uses_contracts_field
  test_lifo_reduce_float_lots_correct
  test_lifo_reduce_removes_lot_below_min_size
  test_recalc_avg_entry_price_float_contracts
"""
from __future__ import annotations

import math
from datetime import date

import pytest

from src.core.models import (
    ActionType, BotState, Decision, Fill, HLAssetMeta, LotRecord, ThesisState,
)
from src.strategy.reduce import lifo_reduce, recalc_avg_entry_price
from tests._helpers import make_lot, make_state


# ── helpers ──────────────────────────────────────────────────────────────────

def _lot(lot_id: str, price: float, contracts: float) -> LotRecord:
    return LotRecord(
        lot_id=lot_id, entry_price=price,
        contracts=contracts, entry_date=date(2026, 1, 1),
    )


# ── LotRecord ────────────────────────────────────────────────────────────────

def test_lot_record_uses_contracts_field():
    """contracts is the primary field; shares is auto-synced."""
    lot = _lot("base", 50000.0, 0.005)
    assert math.isclose(lot.contracts, 0.005)
    # shares is truncated int — 0 for sub-1 contract
    assert lot.shares == 0

    lot2 = _lot("base", 50000.0, 3.0)
    assert math.isclose(lot2.contracts, 3.0)
    assert lot2.shares == 3


def test_lot_record_compat_shares_syncs_contracts():
    """Old-style LotRecord(shares=N) sets contracts=float(N)."""
    lot = make_lot("add_1", entry_price=100.0, shares=8)
    assert math.isclose(lot.contracts, 8.0)
    assert lot.shares == 8


# ── ThesisState ──────────────────────────────────────────────────────────────

def test_thesis_state_contracts_field_defaults_zero():
    """current_position_contracts defaults to 0.0; shares compat defaults to 0."""
    state = ThesisState(symbol="BTC")
    assert state.current_position_contracts == 0.0
    assert state.current_position_shares == 0


def test_thesis_state_syncs_contracts_from_shares():
    """Creating ThesisState(current_position_shares=10) populates contracts=10.0."""
    state = ThesisState(symbol="MU", current_position_shares=10)
    assert math.isclose(state.current_position_contracts, 10.0)
    assert state.current_position_shares == 10


def test_thesis_state_sz_decimals_default():
    state = ThesisState(symbol="BTC")
    assert state.sz_decimals == 0


def test_thesis_state_runner_target_contracts():
    """runner_target_contracts is the primary field; runner_target_shares syncs."""
    state = ThesisState(symbol="BTC", runner_target_contracts=0.25)
    assert math.isclose(state.runner_target_contracts, 0.25)
    assert state.runner_target_shares == 0   # int(0.25) = 0


def test_thesis_state_runner_target_shares_syncs_contracts():
    state = ThesisState(symbol="MU", runner_target_shares=7)
    assert math.isclose(state.runner_target_contracts, 7.0)
    assert state.runner_target_shares == 7


# ── HLAssetMeta ──────────────────────────────────────────────────────────────

def test_hl_asset_meta_validates_correctly():
    meta = HLAssetMeta(
        coin="BTC", sz_decimals=3, min_size=0.001, max_leverage=50.0,
    )
    assert meta.coin == "BTC"
    assert meta.sz_decimals == 3
    assert math.isclose(meta.min_size, 0.001)
    assert math.isclose(meta.max_leverage, 50.0)


# ── Fill / Decision ──────────────────────────────────────────────────────────

def test_fill_uses_contracts_field():
    """Fill.contracts is the primary field; shares compat auto-synced."""
    from datetime import datetime
    fill = Fill(
        action=ActionType.BUY_BASE,
        contracts=0.005,
        fill_price=50000.0,
        slippage_bps=2.5,
        timestamp=datetime.now(),
    )
    assert math.isclose(fill.contracts, 0.005)
    assert fill.shares == 0   # int(0.005) = 0

    fill2 = Fill(
        action=ActionType.BUY_STARTER,
        contracts=10.0,
        fill_price=100.0,
        slippage_bps=1.0,
        timestamp=datetime.now(),
    )
    assert fill2.shares == 10


def test_fill_compat_shares_syncs_contracts():
    """Old-style Fill(shares=10) sets contracts=10.0."""
    from datetime import datetime
    fill = Fill(
        action=ActionType.BUY_BASE,
        shares=10,
        fill_price=100.0,
        slippage_bps=1.0,
        timestamp=datetime.now(),
    )
    assert math.isclose(fill.contracts, 10.0)
    assert fill.shares == 10


def test_decision_uses_contracts_field():
    """Decision.contracts is the primary field; shares compat auto-synced."""
    d = Decision(
        action=ActionType.BUY_ADDON,
        contracts=0.001,
        reason="test",
    )
    assert math.isclose(d.contracts, 0.001)
    assert d.shares == 0   # int(0.001) = 0


def test_decision_compat_shares_syncs_contracts():
    """Old-style Decision(shares=5) sets contracts=5.0."""
    d = Decision(action=ActionType.BUY_ADDON, shares=5, reason="test")
    assert math.isclose(d.contracts, 5.0)
    assert d.shares == 5


# ── lifo_reduce with float lots ──────────────────────────────────────────────

def test_lifo_reduce_float_lots_correct():
    """lifo_reduce works correctly with sub-integer BTC contract sizes."""
    lots = [
        _lot("add_1", 50000.0, 0.010),   # oldest
        _lot("add_2", 52000.0, 0.005),   # latest
    ]
    # Sell 0.005 — should consume add_2 entirely
    updated, sold = lifo_reduce(lots, contracts_to_sell=0.005, sz_decimals=3)
    assert math.isclose(sold, 0.005)
    assert len(updated) == 1
    assert updated[0].lot_id == "add_1"
    assert math.isclose(updated[0].contracts, 0.010)


def test_lifo_reduce_float_lots_partial():
    """Partial reduction of the latest lot preserves correct remaining amount."""
    lots = [
        _lot("add_1", 50000.0, 0.010),
        _lot("add_2", 52000.0, 0.008),
    ]
    # Sell 0.005 — partially reduces add_2 from 0.008 to 0.003
    updated, sold = lifo_reduce(lots, contracts_to_sell=0.005, sz_decimals=3)
    assert math.isclose(sold, 0.005)
    assert len(updated) == 2
    assert updated[1].lot_id == "add_2"
    assert math.isclose(updated[1].contracts, 0.003)


def test_lifo_reduce_removes_lot_below_min_size():
    """A lot whose remaining contracts fall below min_size is dropped entirely."""
    lots = [
        _lot("add_1", 50000.0, 0.010),
        _lot("add_2", 52000.0, 0.0015),   # after selling 0.001 → 0.0005 < min_size=0.001
    ]
    updated, sold = lifo_reduce(
        lots, contracts_to_sell=0.001, sz_decimals=4, min_size=0.001,
    )
    # add_2 was 0.0015, sold 0.001 → remaining 0.0005 < min_size → dropped
    assert len(updated) == 1
    assert updated[0].lot_id == "add_1"
    assert math.isclose(sold, 0.001)


def test_lifo_reduce_backward_compat_shares_kwarg():
    """Old callers passing shares_to_sell=N (keyword) still work."""
    lots = [
        make_lot("add_1", 100.0, 5),
        make_lot("add_2", 110.0, 3),
    ]
    updated, sold = lifo_reduce(lots, shares_to_sell=3)
    assert math.isclose(sold, 3.0)
    assert len(updated) == 1
    assert updated[0].lot_id == "add_1"


# ── recalc_avg_entry_price with float contracts ──────────────────────────────

def test_recalc_avg_entry_price_float_contracts():
    """recalc uses lot.contracts and updates both position fields."""
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=_lot("base", 50000.0, 0.010),
        addon_lots=[_lot("add_1", 52000.0, 0.005)],
    )
    recalc_avg_entry_price(state)

    # Weighted avg = (0.010*50000 + 0.005*52000) / 0.015 = (500+260)/0.015
    expected_avg = (0.010 * 50000.0 + 0.005 * 52000.0) / 0.015
    assert math.isclose(state.avg_entry_price, expected_avg, rel_tol=1e-9)
    assert math.isclose(state.current_position_contracts, 0.015, rel_tol=1e-9)
    # compat: int truncation of 0.015 = 0
    assert state.current_position_shares == 0


def test_recalc_avg_entry_price_integer_contracts_compat():
    """For whole-number contracts (equity path), shares == int(contracts)."""
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 100.0, 10),
        addon_lots=[make_lot("add_1", 110.0, 5)],
    )
    recalc_avg_entry_price(state)

    expected_avg = (10 * 100.0 + 5 * 110.0) / 15
    assert math.isclose(state.avg_entry_price, expected_avg, rel_tol=1e-9)
    assert math.isclose(state.current_position_contracts, 15.0)
    assert state.current_position_shares == 15
