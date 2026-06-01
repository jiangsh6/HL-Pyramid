from __future__ import annotations

import math

from src.core.models import BotState
from src.strategy.sizing import _size, calc_addon_size, calc_entry_size
from tests._helpers import base_config, make_lot, make_state


def test_entry_size_returns_float():
    cfg = base_config()
    qty, blocker = calc_entry_size(
        make_state(), cfg, entry_price=900.0, atr14=10.0,
        intended_exposure_pct=0.08, sz_decimals=0,
    )
    assert blocker is None
    assert isinstance(qty, float)


def test_entry_size_rounds_to_sz_decimals():
    cfg = base_config()
    qty, blocker = calc_entry_size(
        make_state(), cfg, entry_price=50_000.0, atr14=500.0,
        intended_exposure_pct=0.08, sz_decimals=3,
    )
    assert blocker is None
    assert round(qty, 3) == qty


def test_entry_size_allows_fractional_qty_when_notional_is_small():
    cfg = base_config()
    qty, blocker = calc_entry_size(
        make_state(), cfg, entry_price=1_000_000.0, atr14=1.0,
        intended_exposure_pct=0.0001, sz_decimals=0,
    )
    assert blocker is None
    assert math.isclose(qty, 7.5e-06)


def test_addon_size_returns_float():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 900.0, 10),
        avg_entry_price=900.0,
        current_position_qty=10,
        add_count=0,
        trailing_stop_price=800.0,
    )
    qty, blocker = calc_addon_size(state, cfg, add_price=945.0, sz_decimals=0)
    assert blocker is None
    assert isinstance(qty, float)


def test_addon_size_capped_by_remaining_risk_budget():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 50_000.0, 5),
        avg_entry_price=50_000.0,
        current_position_qty=5,
        add_count=0,
        trailing_stop_price=49_000.0,
    )
    qty, blocker = calc_addon_size(state, cfg, add_price=51_000.0, sz_decimals=0)
    assert blocker is None
    assert math.isclose(qty, 6000.0 / 51_000.0)


def test_addon_size_blocked_when_budget_exhausted():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 900.0, 10),
        avg_entry_price=900.0,
        current_position_qty=10,
        add_count=0,
        trailing_stop_price=200.0,
    )
    qty, blocker = calc_addon_size(state, cfg, add_price=945.0, sz_decimals=3)
    assert qty == 0.0
    assert blocker == "thesis_risk_budget_exhausted"


def test_size_does_not_floor_fractional_qty():
    assert _size(6.666, 0) == 6.666
    assert _size(0.00142857, 0) == 0.00142857
    assert _size(0.00142857, 3) == 0.001


def test_entry_size_sz_decimals_3_btc_style():
    cfg = base_config()
    qty, blocker = calc_entry_size(
        make_state(), cfg, entry_price=50_000.0, atr14=1.0,
        intended_exposure_pct=0.08, sz_decimals=3,
    )
    assert blocker is None
    assert qty == 0.12
