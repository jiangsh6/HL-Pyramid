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
    shares, blocker = calc_entry_size(
        state, cfg, entry_price=900.0, atr14=10.0, intended_exposure_pct=0.08,
    )
    assert blocker is None
    # Risk per share: stop_pct=0.12 → pct_stop=792; atr_stop=900-2.5*10=875; min=792.
    # risk_per_share = 900 - 792 = 108
    # max_total_risk_budget = 100_000 * 0.06 = 6_000
    # shares_by_risk = floor(6000/108) = 55
    # target_notional = 0.08 * 75_000 = 6_000
    # shares_by_exposure = floor(6000/900) = 6
    # final = min(55, 6) = 6
    assert shares == 6


def test_entry_size_blocks_when_position_size_zero():
    cfg = base_config()
    state = make_state()
    # Force tiny exposure so floor yields 0
    shares, blocker = calc_entry_size(
        state, cfg, entry_price=1_000_000.0, atr14=1.0,
        intended_exposure_pct=0.0001,
    )
    assert shares == 0
    assert blocker == "position_size_zero"


def test_addon_size_uses_remaining_risk_budget():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", entry_price=900.0, shares=10),
        addon_lots=[],
        avg_entry_price=900.0,
        current_position_shares=10,
        add_count=0,
        trailing_stop_price=800.0,
    )
    shares, blocker = calc_addon_size(state, cfg, add_price=945.0)
    assert blocker is None
    # max_risk_budget = 6000
    # current_open_risk = (900-800)*10 = 1000
    # remaining = 5000
    # incremental = 945-800 = 145
    # shares_by_risk = floor(5000/145) = 34
    # target_notional = 0.08 * 75_000 = 6000
    # shares_by_exposure = floor(6000/945) = 6
    # final = min(34, 6) = 6
    assert shares == 6


def test_addon_size_blocks_when_remaining_budget_exhausted():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", entry_price=900.0, shares=10),
        avg_entry_price=900.0,
        current_position_shares=10,
        add_count=0,
        trailing_stop_price=200.0,   # huge open risk
    )
    shares, blocker = calc_addon_size(state, cfg, add_price=945.0)
    assert shares == 0
    assert blocker == "thesis_risk_budget_exhausted"


def test_addon_size_blocks_when_stop_above_price():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", entry_price=900.0, shares=10),
        avg_entry_price=900.0,
        current_position_shares=10,
        add_count=0,
        trailing_stop_price=1000.0,   # above add_price
    )
    shares, blocker = calc_addon_size(state, cfg, add_price=945.0)
    assert shares == 0
    assert blocker == "stop_above_entry_price"


def test_addon_size_blocks_without_trailing_stop():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", entry_price=900.0, shares=10),
        avg_entry_price=900.0,
        current_position_shares=10,
        add_count=0,
        trailing_stop_price=None,
    )
    shares, blocker = calc_addon_size(state, cfg, add_price=945.0)
    assert shares == 0
    assert blocker == "trailing_stop_unset"
