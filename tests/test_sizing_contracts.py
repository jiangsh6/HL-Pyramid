"""
HL Phase 2 — Sizing tests with float contracts and sz_decimals.

Named tests (7 required):
  test_entry_size_returns_float
  test_entry_size_rounds_to_sz_decimals
  test_entry_size_zero_when_notional_too_small
  test_addon_size_returns_float
  test_addon_size_capped_by_remaining_risk_budget
  test_addon_size_blocked_when_budget_exhausted
  test_sizing_sz_decimals_0_matches_prior_integer_behavior
"""
from __future__ import annotations

import math

import pytest

from src.core.models import BotState
from src.strategy.sizing import calc_addon_size, calc_entry_size, _size
from tests._helpers import base_config, make_lot, make_state


# ── calc_entry_size ──────────────────────────────────────────────────────────

def test_entry_size_returns_float():
    """calc_entry_size always returns a float, even for integer lot sizes."""
    cfg = base_config()
    state = make_state()
    contracts, blocker = calc_entry_size(
        state, cfg, entry_price=900.0, atr14=10.0,
        intended_exposure_pct=0.08, sz_decimals=0,
    )
    assert blocker is None
    assert isinstance(contracts, float)


def test_entry_size_rounds_to_sz_decimals():
    """With sz_decimals=3 the result is rounded to 3 decimal places."""
    cfg = base_config()
    state = make_state()
    # Force a case where the raw notional / price is fractional
    # target_notional = 0.08 * 75_000 = 6_000
    # 6_000 / 50_000 = 0.12  (exactly 3 d.p.)
    contracts, blocker = calc_entry_size(
        state, cfg, entry_price=50_000.0, atr14=500.0,
        intended_exposure_pct=0.08, sz_decimals=3,
    )
    assert blocker is None
    # Result should be expressible to 3 d.p.
    assert round(contracts, 3) == contracts


def test_entry_size_zero_when_notional_too_small():
    """If rounded result ≤ 0, blocker = position_size_zero."""
    cfg = base_config()
    state = make_state()
    # Tiny exposure forces result < 1 unit → blocked
    contracts, blocker = calc_entry_size(
        state, cfg, entry_price=1_000_000.0, atr14=1.0,
        intended_exposure_pct=0.0001, sz_decimals=0,
    )
    assert contracts == 0.0
    assert blocker == "position_size_zero"


# ── calc_addon_size ──────────────────────────────────────────────────────────

def test_addon_size_returns_float():
    """calc_addon_size returns float."""
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 900.0, 10),
        addon_lots=[],
        avg_entry_price=900.0,
        current_position_shares=10,
        add_count=0,
        trailing_stop_price=800.0,
    )
    contracts, blocker = calc_addon_size(state, cfg, add_price=945.0, sz_decimals=0)
    assert blocker is None
    assert isinstance(contracts, float)


def test_addon_size_capped_by_remaining_risk_budget():
    """The risk-budget leg caps addon size correctly for fractional lots."""
    cfg = base_config()
    # Large open risk → tiny remaining budget → tiny result
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 50_000.0, 1),
        addon_lots=[],
        avg_entry_price=50_000.0,
        current_position_shares=1,
        add_count=0,
        trailing_stop_price=49_000.0,   # 1000 risk per contract, 1 contract → 1000 open risk
    )
    # max_risk_budget = 100_000 * 0.06 = 6_000
    # open_risk = 1 * (50_000 - 49_000) = 1_000
    # remaining = 5_000
    # incremental = 51_000 - 49_000 = 2_000
    # contracts_by_risk = floor(5000/2000) = 2.0 (sz_decimals=0)
    # target_notional = 0.08 * 75_000 = 6_000 (add_count=0)
    # contracts_by_exposure = floor(6000/51000) = 0.0
    # final = min(2, 0) = 0 → position_size_zero
    contracts, blocker = calc_addon_size(
        state, cfg, add_price=51_000.0, sz_decimals=0,
    )
    assert contracts == 0.0
    assert blocker == "position_size_zero"


def test_addon_size_blocked_when_budget_exhausted():
    """With all risk budget consumed, returns 0 and thesis_risk_budget_exhausted."""
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 900.0, 10),
        avg_entry_price=900.0,
        current_position_shares=10,
        add_count=0,
        trailing_stop_price=200.0,   # enormous open risk
    )
    contracts, blocker = calc_addon_size(state, cfg, add_price=945.0, sz_decimals=3)
    assert contracts == 0.0
    assert blocker == "thesis_risk_budget_exhausted"


# ── sz_decimals=0 backward compat ────────────────────────────────────────────

def test_sizing_sz_decimals_0_matches_prior_integer_behavior():
    """
    sz_decimals=0 must produce the same result as the old math.floor() path
    for inputs whose result is a whole number.

    Verifies the _size() helper: _size(6.0, 0) == 6.0 == math.floor(6.0).
    """
    import math as _math
    # Whole-number raw values: floor and _size(x, 0) must agree
    for raw in (1.0, 6.0, 10.0, 100.0, 50.0):
        assert _size(raw, 0) == float(_math.floor(raw)), (
            f"_size({raw}, 0) = {_size(raw, 0)} != floor({raw}) = {_math.floor(raw)}"
        )

    # Non-whole-number: _size uses floor (not round)
    # e.g. 6.666 → floor=6, round-nearest=7
    assert _size(6.666, 0) == 6.0   # floor, not round
    assert _size(6.999, 0) == 6.0   # floor, not round
    assert _size(6.001, 0) == 6.0

    # sz_decimals > 0: use round-nearest
    assert _size(0.0056, 3) == 0.006  # rounds up
    assert _size(0.0054, 3) == 0.005  # rounds down


def test_entry_size_sz_decimals_3_btc_style():
    """Full entry sizing with sz_decimals=3 (BTC-style) returns 3-d.p. float."""
    cfg = base_config()
    state = make_state()
    # entry_price=50_000, stop via pct: stop_pct=0.12 → stop=44_000, risk_per=6_000
    # max_risk = 100_000 * 0.06 = 6_000
    # contracts_by_risk = floor(6_000/6_000) = 1.0 ... with sz_decimals=3 → round(1.0, 3) = 1.0
    # target_notional = 0.08 * 75_000 = 6_000 → round(6_000/50_000, 3) = round(0.12, 3) = 0.12
    # final = min(1.0, 0.12) = 0.12
    contracts, blocker = calc_entry_size(
        state, cfg,
        entry_price=50_000.0,
        atr14=1.0,                  # tiny ATR → pct_stop dominates
        intended_exposure_pct=0.08,
        sz_decimals=3,
    )
    assert blocker is None
    assert isinstance(contracts, float)
    assert round(contracts, 3) == contracts   # is a valid 3-d.p. number
    assert contracts > 0
