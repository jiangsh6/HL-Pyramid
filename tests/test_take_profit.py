"""Section 22.7 — Take profit tests."""
import math

from src.core.models import ActionType, BotState
from src.strategy.take_profit import (
    check_exposure_tp, check_giveback_tp, check_layered_tp,
    check_target_tp, check_trailing_tp,
)
from tests._helpers import base_config, make_indicators, make_lot, make_state


def _base_state(**overrides):
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 700.0, 30),
        avg_entry_price=700.0,
        current_position_qty=30,
        original_base_qty=30,
        highest_price_since_entry=1000.0,
    )
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


def test_layered_tp_triggers_at_correct_profit_level():
    cfg = base_config()
    state = _base_state()
    # +30% from avg 700 → 910
    ind = make_indicators(adj_close=910.0)
    d = check_layered_tp(state, ind, cfg)
    assert d is not None
    assert d.action == ActionType.SELL_TAKE_PROFIT
    assert d.reason == "layered_tp_level_1"
    assert d.qty == 3   # 


def test_layered_tp_triggers_only_once_per_level():
    cfg = base_config()
    state = _base_state()
    ind = make_indicators(adj_close=910.0)
    d1 = check_layered_tp(state, ind, cfg)
    assert d1 is not None
    # Now state.tp_levels_triggered[0] = True
    d2 = check_layered_tp(state, ind, cfg)
    assert d2 is None


def test_only_highest_tp_level_fires_on_gap_up_bar():
    cfg = base_config()
    state = _base_state()
    # +55% gap (350 + 700 → 1085 etc).  avg=700, close=1085 → 55%
    ind = make_indicators(adj_close=1085.0)
    d = check_layered_tp(state, ind, cfg)
    assert d.reason == "layered_tp_level_2"   # 50% tier


def test_all_lower_tp_levels_marked_triggered_on_gap_up():
    cfg = base_config()
    state = _base_state()
    ind = make_indicators(adj_close=1085.0)   # +55%
    check_layered_tp(state, ind, cfg)
    assert state.tp_levels_triggered[0] is True   # 30%
    assert state.tp_levels_triggered[1] is True   # 50%
    assert state.tp_levels_triggered[2] is False
    assert state.tp_levels_triggered[3] is False


def test_layered_tp_reduces_addons_first_lifo():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 700.0, 20),
        addon_lots=[
            make_lot("add_1", 745.0, 8),
            make_lot("add_2", 800.0, 6),
        ],
        avg_entry_price=727.0,
        current_position_qty=34,
        highest_price_since_entry=950.0,
    )
    ind = make_indicators(adj_close=950.0)   # ~30% from 727
    d = check_layered_tp(state, ind, cfg)
    assert d is not None
    assert math.isclose(d.qty, 3.4)


def test_trailing_stop_raised_after_layered_tp():
    cfg = base_config()
    state = _base_state(trailing_stop_price=600.0)
    ind = make_indicators(adj_close=910.0)
    check_layered_tp(state, ind, cfg)
    # Level 1 move_stop_to = breakeven (avg=700)
    assert state.trailing_stop_price == 700.0


def test_target_price_tp_fires_from_base_long_state():
    cfg = base_config()
    state = _base_state()
    ind = make_indicators(adj_close=2000.0)   # at target
    d = check_target_tp(state, ind, cfg)
    assert d is not None
    assert d.action == ActionType.SELL_TAKE_PROFIT
    assert d.new_state == BotState.RUNNER_LONG   # runner_mode enabled


def test_target_price_tp_fires_from_event_risk_mode():
    cfg = base_config()
    state = _base_state(state=BotState.EVENT_RISK_MODE,
                        prior_state=BotState.BASE_LONG)
    ind = make_indicators(adj_close=2050.0)
    d = check_target_tp(state, ind, cfg)
    assert d is not None
    assert d.new_state == BotState.RUNNER_LONG


def test_target_price_tp_uses_target_price_tp_triggered_field():
    cfg = base_config()
    state = _base_state()
    ind = make_indicators(adj_close=2000.0)
    d1 = check_target_tp(state, ind, cfg)
    assert d1 is not None
    assert state.target_price_tp_triggered is True
    # Second call — does not re-fire
    d2 = check_target_tp(state, ind, cfg)
    assert d2 is None


def test_target_price_tp_enters_runner_mode():
    cfg = base_config()
    state = _base_state()   # base_lot.qty=30
    ind = make_indicators(adj_close=2000.0)
    d = check_target_tp(state, ind, cfg)
    assert d is not None
    assert state.runner_target_qty == 7.5
    assert state.runner_mode_active is True
    # Tight trailing stop set to adj_close * (1 - 0.08) = 1840
    assert abs(state.trailing_stop_price - 1840.0) < 1e-6


def test_runner_mode_uses_original_base_qty_after_base_reduction():
    cfg = base_config()
    state = _base_state(
        base_lot=make_lot("base", 700.0, 0.5),
        current_position_qty=0.5,
        original_base_qty=1.0,
    )
    ind = make_indicators(adj_close=2000.0)
    d = check_target_tp(state, ind, cfg)
    assert d is not None
    assert d.action == ActionType.SELL_TAKE_PROFIT
    assert state.runner_target_qty == 0.25
    assert d.qty == 0.25


def test_runner_mode_halts_when_original_base_qty_missing():
    cfg = base_config()
    state = _base_state(original_base_qty=0.0)
    ind = make_indicators(adj_close=2000.0)
    d = check_target_tp(state, ind, cfg)
    assert d is not None
    assert d.action == ActionType.HALT
    assert state.halted is True
    assert state.halt_reason == "original_base_qty_missing_for_runner"


def test_exposure_tp_triggers_at_hard_cap():
    cfg = base_config()
    # hard_cap=1.00 → 100k notional on 100k equity → 1000 qty at $100
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 80.0, 1000),
        avg_entry_price=80.0,
        current_position_qty=1000,
    )
    ind = make_indicators(adj_close=100.0)
    d = check_exposure_tp(state, ind, cfg)
    assert d is not None
    # reduce to 70% → 700 qty → sell 300
    assert d.qty == 300


def test_exposure_tp_soft_cap_blocks_add_only():
    cfg = base_config()
    # At soft cap (80%) but not hard cap (100%) → no TP sell, but add should be blocked
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 80.0, 850),
        avg_entry_price=80.0,
        current_position_qty=850,
    )
    ind = make_indicators(adj_close=100.0)   # 85k notional → 85% exposure
    d = check_exposure_tp(state, ind, cfg)
    assert d is None   # no TP


def test_profit_giveback_tp_triggers_when_giveback_exceeds_threshold():
    cfg = base_config()
    state = _base_state(addon_lots=[make_lot("add_1", 800.0, 5)])
    state.current_position_qty = 35
    state.avg_entry_price = (30 * 700 + 5 * 800) / 35
    # Set peak 25% of equity ($25k unrealized) — current 4% giveback well above 25% threshold
    state.peak_unrealized_pnl_pct = 0.25
    # current unrealized pnl: at adj_close X, (X - avg) * 35 / 100_000
    # Want unrealized_pct = 0.10 → (X-avg)*35 = 10000 → X = avg + 285.71
    ind = make_indicators(adj_close=state.avg_entry_price + 286.0)
    # giveback = (0.25-0.10)/0.25 = 0.60 > 0.25 max for the 0.20 tier
    d = check_giveback_tp(state, ind, cfg)
    assert d is not None
    assert d.action == ActionType.SELL_TAKE_PROFIT


def test_trailing_tp_fires_at_correct_profit_and_drawdown_tier():
    cfg = base_config()
    state = _base_state(
        addon_lots=[make_lot("add_1", 800.0, 10)],
        avg_entry_price=725.0,
        current_position_qty=40,
        highest_price_since_entry=1050.0,
    )
    # profit_from_avg: at close 900, (900-725)/725 = 24% → first tier (20%)
    # drawdown: (1050-900)/1050 = 14.3% > tier trailing_pct 12%
    ind = make_indicators(adj_close=900.0)
    d = check_trailing_tp(state, ind, cfg)
    assert d is not None
    assert d.reason == "trailing_tp_reduce_addons_50pct"
    assert d.qty == 5   # 


def test_runner_mode_uses_separate_tight_trailing_stop():
    cfg = base_config()
    state = _base_state(state=BotState.RUNNER_LONG,
                        runner_mode_active=True,
                        runner_target_qty=7,
                        trailing_stop_price=1840.0)
    state.base_lot = make_lot("base", 700.0, 7)
    state.current_position_qty = 7
    # adj_close=1840 ⇒ trailing trips; close ≤ stop
    from src.strategy.stops import check_stop_triggers
    ind = make_indicators(adj_close=1840.0, high=1850.0, ma5=1800, ma10=1750,
                          ma20=1700, ma50=1600)
    trig = check_stop_triggers(state, ind, cfg)
    assert trig == "trailing_stop"
