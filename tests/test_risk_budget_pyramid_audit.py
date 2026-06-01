from __future__ import annotations

from src.core.decision_engine import run
from src.core.models import ActionType, BotState
from src.reporting.daily_summary import format_summary
from src.strategy.add import check_add_conditions
from src.strategy.sizing import calc_addon_size
from tests._helpers import base_config, make_indicators, make_lot, make_state


def _over_budget_pyramid_state():
    return make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", entry_price=900.0, qty=10),
        addon_lots=[],
        avg_entry_price=900.0,
        current_position_qty=10.0,
        add_count=0,
        trailing_stop_price=200.0,
        last_add_price=900.0,
        entry_date=None,
    )


def test_daily_summary_reports_open_risk_above_max_with_zero_remaining_budget():
    cfg = base_config()
    state = _over_budget_pyramid_state()
    indicators = make_indicators(adj_close=946.0, close=946.0, ma10=900.0, ma20=850.0)

    summary = format_summary(state, indicators, cfg)

    assert "Max thesis risk:     $6,000" in summary
    assert "Open risk (all lots): $7,000" in summary
    assert "Remaining budget:    $0" in summary


def test_addon_size_blocks_when_existing_open_risk_exceeds_budget():
    cfg = base_config()
    state = _over_budget_pyramid_state()

    qty, blocker = calc_addon_size(state, cfg, add_price=946.0)

    assert qty == 0.0
    assert blocker == "thesis_risk_budget_exhausted"


def test_decision_engine_blocks_pyramid_add_when_existing_open_risk_exceeds_budget():
    cfg = base_config()
    state = _over_budget_pyramid_state()
    indicators = make_indicators(
        adj_close=946.0,
        close=946.0,
        ma10=900.0,
        ma20=850.0,
        prior_highest_high_20d=930.0,
        distance_from_ma10=0.02,
        distance_from_ma20=0.05,
        intraday_return=0.01,
    )

    decision = run(state, indicators, cfg)

    assert decision.action == ActionType.NO_ACTION
    assert "thesis_risk_budget_exhausted" in decision.blockers


def test_add_conditions_block_pyramid_add_at_max_add_count():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", entry_price=900.0, qty=1),
        avg_entry_price=900.0,
        current_position_qty=1.0,
        add_count=cfg.add["max_add_count"],
        trailing_stop_price=890.0,
        last_add_price=900.0,
        entry_date=None,
    )
    indicators = make_indicators(
        adj_close=946.0,
        close=946.0,
        ma10=900.0,
        ma20=850.0,
        prior_highest_high_20d=930.0,
    )

    decision = check_add_conditions(state, indicators, cfg)

    assert decision.action == ActionType.NO_ACTION
    assert "max_add_count_reached" in decision.blockers
