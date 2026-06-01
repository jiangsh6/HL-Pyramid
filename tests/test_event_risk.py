"""Section 22.9 — Event risk tests."""
from datetime import date

from src.core.models import ActionType, BotState
from src.core.decision_engine import run
from src.strategy.event_risk import (
    calc_trading_days_to_event, check_event_derisking, update_event_risk_mode,
)
from tests._helpers import base_config, make_indicators, make_lot, make_state


def _config_with_event_date(event_iso: str):
    cfg = base_config()
    cfg.thesis["event_date"] = event_iso
    return cfg


def test_calc_trading_days_to_event():
    # 2026-06-01 → 2026-06-08 is 5 business days (Mon-Fri then Mon)
    days = calc_trading_days_to_event(date(2026, 6, 1), date(2026, 6, 8))
    assert days == 5


def test_event_t10_transitions_to_event_risk_mode_only():
    # event_date 10 business days away
    # 2026-06-08 - 10 bdays = 2026-05-25
    cfg = _config_with_event_date("2026-06-08")
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 900.0, 30),
        avg_entry_price=900.0,
        current_position_qty=30,
        highest_price_since_entry=1000.0,
        trailing_stop_price=800.0,
    )
    ind = make_indicators(date=date(2026, 5, 25), adj_close=950.0,
                          ma5=940, ma10=930, ma20=920, ma50=900)
    update_event_risk_mode(state, ind, cfg)
    assert state.state == BotState.EVENT_RISK_MODE
    assert state.prior_state == BotState.BASE_LONG
    assert state.days_to_event == 10


def test_event_t10_does_not_generate_orders():
    """At T-10, only mode transition happens. Step 9 de-risking returns None."""
    cfg = _config_with_event_date("2026-06-08")
    state = make_state(
        state=BotState.EVENT_RISK_MODE,
        prior_state=BotState.BASE_LONG,
        base_lot=make_lot("base", 900.0, 5),
        avg_entry_price=900.0,
        current_position_qty=5,  # low exposure → no de-risking
        highest_price_since_entry=900.0,
        trailing_stop_price=800.0,
        days_to_event=10,
    )
    ind = make_indicators(date=date(2026, 5, 25), adj_close=950.0,
                          ma5=940, ma10=930, ma20=920, ma50=900)
    d = check_event_derisking(state, ind, cfg)
    assert d is None


def test_event_t5_de_risking_generates_sell_order():
    cfg = _config_with_event_date("2026-06-08")
    state = make_state(
        state=BotState.EVENT_RISK_MODE,
        prior_state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 800),
        avg_entry_price=100.0,
        current_position_qty=800,  # 80k exposure → 80%
        highest_price_since_entry=110.0,
        trailing_stop_price=90.0,
    )
    # 2026-06-01 → 5 bdays before 2026-06-08
    ind = make_indicators(date=date(2026, 6, 1), adj_close=100.0,
                          ma5=99, ma10=99, ma20=99, ma50=95)
    d = check_event_derisking(state, ind, cfg)
    assert d is not None
    assert d.action == ActionType.SELL_EVENT_DERISKING
    # reduce to 50% → 500 qty → sell 300
    assert d.qty == 300


def test_event_t2_reduces_to_core_20pct():
    cfg = _config_with_event_date("2026-06-08")
    state = make_state(
        state=BotState.EVENT_RISK_MODE,
        prior_state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 500),
        avg_entry_price=100.0,
        current_position_qty=500,
        highest_price_since_entry=110.0,
        trailing_stop_price=90.0,
    )
    # 2026-06-04 → 2 bdays before 2026-06-08
    ind = make_indicators(date=date(2026, 6, 4), adj_close=100.0,
                          ma5=99, ma10=99, ma20=99, ma50=95)
    d = check_event_derisking(state, ind, cfg)
    assert d is not None
    # reduce to 20% → 200 qty → sell 300
    assert d.qty == 300


def test_event_t1_force_flat_if_configured():
    cfg = _config_with_event_date("2026-06-08")
    cfg.event_risk["force_flat_before_event"] = True
    cfg.event_risk["force_flat_trading_days_before"] = 1
    state = make_state(
        state=BotState.EVENT_RISK_MODE,
        prior_state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 200),
        avg_entry_price=100.0,
        current_position_qty=200,
        highest_price_since_entry=110.0,
        trailing_stop_price=90.0,
    )
    # 2026-06-05 → 1 bday before 2026-06-08
    ind = make_indicators(date=date(2026, 6, 5), adj_close=100.0,
                          ma5=99, ma10=99, ma20=99, ma50=95)
    d = check_event_derisking(state, ind, cfg)
    assert d is not None
    assert d.qty == 200
    assert d.new_state == BotState.EXITED


def test_stop_overrides_event_derisking_on_same_bar():
    """If hard stop fires Step 6, event de-risking Step 9 must NOT execute."""
    cfg = _config_with_event_date("2026-06-08")
    state = make_state(
        state=BotState.EVENT_RISK_MODE,
        prior_state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 500),
        avg_entry_price=100.0,
        current_position_qty=500,
        initial_stop_price=80.0,
        trailing_stop_price=90.0,
        highest_price_since_entry=110.0,
    )
    # T-2 (would normally fire), but adj_close=75 triggers hard stop
    ind = make_indicators(date=date(2026, 6, 4), adj_close=75.0,
                          ma5=80, ma10=80, ma20=80, ma50=80)
    d = run(state, ind, cfg)
    assert d.action == ActionType.SELL_STOP
    assert d.new_state == BotState.EXITED


def test_post_event_cooldown_restores_prior_state():
    cfg = _config_with_event_date("2026-06-08")
    state = make_state(
        state=BotState.EVENT_RISK_MODE,
        prior_state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 100.0, 20),
        addon_lots=[make_lot("add_1", 105.0, 5)],
        avg_entry_price=101.0,
        current_position_qty=25,
        highest_price_since_entry=110.0,
        trailing_stop_price=90.0,
    )
    # 2026-06-10 = 2 bdays after event 2026-06-08; cooldown=1
    ind = make_indicators(date=date(2026, 6, 10), adj_close=105.0,
                          ma5=100, ma10=100, ma20=99, ma50=95)
    update_event_risk_mode(state, ind, cfg)
    assert state.state == BotState.PYRAMID_LONG
    assert state.prior_state is None


def test_post_event_add_count_reset():
    cfg = _config_with_event_date("2026-06-08")
    state = make_state(
        state=BotState.EVENT_RISK_MODE,
        prior_state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 100.0, 20),
        addon_lots=[make_lot("add_1", 105.0, 5)],
        avg_entry_price=101.0,
        current_position_qty=25,
        add_count=3,
        highest_price_since_entry=110.0,
        trailing_stop_price=90.0,
    )
    ind = make_indicators(date=date(2026, 6, 10), adj_close=105.0,
                          ma5=100, ma10=100, ma20=99, ma50=95)
    update_event_risk_mode(state, ind, cfg)
    assert state.add_count == 0


def test_protect_profit_mode_cleared_after_event_reset():
    cfg = _config_with_event_date("2026-06-08")
    state = make_state(
        state=BotState.EVENT_RISK_MODE,
        prior_state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 100.0, 20),
        addon_lots=[make_lot("add_1", 105.0, 5)],
        avg_entry_price=101.0,
        current_position_qty=25,
        add_count=4,
        protect_profit_mode=True,
    )
    ind = make_indicators(date=date(2026, 6, 10), adj_close=105.0,
                          ma5=100, ma10=100, ma20=99, ma50=95)
    update_event_risk_mode(state, ind, cfg)
    assert state.protect_profit_mode is False
