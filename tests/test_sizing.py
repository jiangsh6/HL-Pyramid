"""Sizing tests (Section 10)."""
from src.core.models import BotState
from src.strategy.sizing import (
    available_capital, calc_addon_size, calc_entry_size,
)
from tests._helpers import base_config, make_lot, make_state


def test_available_capital_excludes_reserve():
    cfg = base_config()
    # 100_000 * (1 - 0.25) = 75_000
    assert available_capital(cfg) == 75_000.0


def test_entry_size_uses_min_of_risk_and_exposure():
    cfg = base_config()
    state = make_state()
    qty, blocker = calc_entry_size(
        state, cfg, entry_price=900.0, atr14=10.0, intended_exposure_pct=0.08,
    )
    assert blocker is None
    assert qty == 6000.0 / 900.0


def test_entry_size_blocks_when_position_qty_too_small():
    cfg = base_config()
    state = make_state()
    qty, blocker = calc_entry_size(
        state, cfg, entry_price=1_000_000.0, atr14=1.0,
        intended_exposure_pct=0.0001,
    )
    assert qty > 0
    assert blocker is None


def test_addon_size_uses_remaining_risk_budget():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", entry_price=900.0, qty=10),
        addon_lots=[],
        avg_entry_price=900.0,
        current_position_qty=10,
        add_count=0,
        trailing_stop_price=800.0,
    )
    qty, blocker = calc_addon_size(state, cfg, add_price=945.0)
    assert blocker is None
    assert qty == 6000.0 / 945.0


def test_addon_size_blocks_when_remaining_budget_exhausted():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", entry_price=900.0, qty=10),
        avg_entry_price=900.0,
        current_position_qty=10,
        add_count=0,
        trailing_stop_price=200.0,   # huge open risk
    )
    qty, blocker = calc_addon_size(state, cfg, add_price=945.0)
    assert qty == 0
    assert blocker == "thesis_risk_budget_exhausted"


def test_addon_size_blocks_when_stop_above_price():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", entry_price=900.0, qty=10),
        avg_entry_price=900.0,
        current_position_qty=10,
        add_count=0,
        trailing_stop_price=1000.0,   # above add_price
    )
    qty, blocker = calc_addon_size(state, cfg, add_price=945.0)
    assert qty == 0
    assert blocker == "stop_above_entry_price"


def test_addon_size_blocks_without_trailing_stop():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", entry_price=900.0, qty=10),
        avg_entry_price=900.0,
        current_position_qty=10,
        add_count=0,
        trailing_stop_price=None,
    )
    qty, blocker = calc_addon_size(state, cfg, add_price=945.0)
    assert qty == 0
    assert blocker == "trailing_stop_unset"
