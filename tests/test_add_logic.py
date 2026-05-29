"""Section 22.5 — Add tests."""
from datetime import date, timedelta

from src.core.models import ActionType, BotState
from src.strategy.add import check_add_conditions
from src.core.decision_engine import run
from tests._helpers import base_config, make_indicators, make_lot, make_state


def _add_ready_state(**overrides):
    """Build a state that satisfies most add conditions."""
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", entry_price=900.0, shares=10,
                          entry_date_str="2026-05-20"),
        avg_entry_price=900.0,
        current_position_shares=10,
        entry_date=date(2026, 5, 20),
        last_add_price=900.0,
        add_count=0,
        trailing_stop_price=800.0,
    )
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


def _ready_indicators(**overrides):
    """Indicators where add would otherwise fire."""
    ind_kw = dict(
        date=date(2026, 5, 27),
        adj_close=950.0,          # 5.5% above avg 900, >= 5% step
        ma5=940, ma10=920, ma20=910, ma50=890,
        distance_from_ma10=0.03, distance_from_ma20=0.04,
        intraday_return=0.01, gap_up_pct=0.0,
    )
    ind_kw.update(overrides)
    return make_indicators(**ind_kw)


def test_no_add_when_position_losing():
    cfg = base_config()
    state = _add_ready_state()
    ind = _ready_indicators(adj_close=850.0)  # below avg → losing
    d = check_add_conditions(state, ind, cfg)
    assert d is None or d.action != ActionType.BUY_ADDON


def test_no_add_before_cooldown_days():
    cfg = base_config()
    state = _add_ready_state(entry_date=date(2026, 5, 26))   # only 1 day ago
    ind = _ready_indicators()
    d = check_add_conditions(state, ind, cfg)
    assert d.action == ActionType.NO_ACTION
    assert "entry_cooldown" in d.blockers


def test_no_add_when_profit_below_threshold():
    cfg = base_config()
    state = _add_ready_state()
    # 2% gain — below 4% threshold
    ind = _ready_indicators(adj_close=918.0)
    d = check_add_conditions(state, ind, cfg)
    assert d.action == ActionType.NO_ACTION


def test_no_add_when_close_below_ma10():
    cfg = base_config()
    state = _add_ready_state()
    ind = _ready_indicators(adj_close=950.0, ma10=960.0)
    d = check_add_conditions(state, ind, cfg)
    assert "close_below_ma10" in d.blockers


def test_no_add_when_ma10_below_ma20():
    cfg = base_config()
    state = _add_ready_state()
    ind = _ready_indicators(ma10=910, ma20=920)
    d = check_add_conditions(state, ind, cfg)
    assert "ma10_below_ma20" in d.blockers


def test_no_add_when_add_count_maxed():
    cfg = base_config()
    state = _add_ready_state(add_count=4, state=BotState.PYRAMID_LONG,
                             protect_profit_mode=True)
    ind = _ready_indicators()
    d = check_add_conditions(state, ind, cfg)
    assert d.action == ActionType.NO_ACTION
    assert ("max_add_count_reached" in d.blockers
            or "protect_profit_mode" in d.blockers)


def test_no_add_inside_event_window():
    cfg = base_config()
    state = _add_ready_state(days_to_event=8)
    ind = _ready_indicators()
    d = check_add_conditions(state, ind, cfg)
    assert "event_window" in d.blockers


def test_no_add_when_protect_profit_mode_true():
    cfg = base_config()
    state = _add_ready_state(protect_profit_mode=True)
    ind = _ready_indicators()
    d = check_add_conditions(state, ind, cfg)
    assert "protect_profit_mode" in d.blockers


def test_first_add_trigger_uses_avg_entry_as_last_add_price():
    cfg = base_config()
    # last_add_price was set to avg_entry_price at base entry; trigger needs
    # close >= last_add_price * (1 + add_trigger_pct).  900 * 1.05 = 945.
    state = _add_ready_state(last_add_price=900.0)
    # 944.99 < 945 — should NOT add
    ind = _ready_indicators(adj_close=944.0)
    d = check_add_conditions(state, ind, cfg)
    assert d.action == ActionType.NO_ACTION
    assert "price_step_not_met" in d.blockers

    # 945.01 ≥ 945 — should add
    ind2 = _ready_indicators(adj_close=946.0)
    d2 = check_add_conditions(state, ind2, cfg)
    assert d2.action == ActionType.BUY_ADDON


def test_add_when_all_conditions_met():
    cfg = base_config()
    state = _add_ready_state()
    ind = _ready_indicators(adj_close=946.0)
    d = check_add_conditions(state, ind, cfg)
    assert d.action == ActionType.BUY_ADDON


def test_add_blocked_when_remaining_risk_budget_exhausted():
    cfg = base_config()
    # All risk budget consumed by huge gap between entry and stop
    state = _add_ready_state(trailing_stop_price=200.0)
    ind = _ready_indicators(adj_close=946.0)
    d = run(state, ind, cfg)
    assert d.action == ActionType.NO_ACTION
    assert "thesis_risk_budget_exhausted" in d.blockers


def test_add_blocked_when_tp_fired_same_cycle():
    """Section 8.1 priority — TP fires Step 10, blocks add at Step 12."""
    cfg = base_config()
    state = _add_ready_state(avg_entry_price=700.0)
    # Profit = (950-700)/700 = 35.7% — triggers layered TP level 1 (30%).
    ind = _ready_indicators(adj_close=950.0)
    d = run(state, ind, cfg)
    assert d.action == ActionType.SELL_TAKE_PROFIT
