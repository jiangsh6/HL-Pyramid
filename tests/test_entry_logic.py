"""Section 22.4 — Entry tests."""
from datetime import date

from src.core.models import ActionType, BotState
from src.strategy.entry import (
    check_breakout_base, check_pullback_base, check_starter_entry,
)
from tests._helpers import base_config, make_indicators, make_state


def test_no_entry_when_thesis_disabled():
    cfg = base_config()
    state = make_state(thesis_enabled=False)
    ind = make_indicators(adj_close=100.0, ma20=95.0)
    assert check_starter_entry(state, ind, cfg) is None


def test_no_entry_when_close_below_ma20():
    cfg = base_config()
    state = make_state()
    ind = make_indicators(adj_close=94.0, ma20=95.0)
    assert check_starter_entry(state, ind, cfg) is None


def test_no_entry_when_intraday_gain_too_large():
    cfg = base_config()
    state = make_state()
    # Starter max_intraday_gain_pct=0.08
    ind = make_indicators(
        adj_close=100.0, ma20=95.0, intraday_return=0.09,
    )
    assert check_starter_entry(state, ind, cfg) is None


def test_no_entry_when_gap_up_too_large():
    cfg = base_config()
    state = make_state()
    # Starter max_gap_up_pct=0.06
    ind = make_indicators(
        adj_close=100.0, ma20=95.0, gap_up_pct=0.08,
    )
    assert check_starter_entry(state, ind, cfg) is None


def test_starter_entry_when_all_conditions_met():
    cfg = base_config()
    state = make_state()
    ind = make_indicators(
        adj_close=100.0, ma5=99, ma10=98, ma20=95, ma50=90,
        distance_from_ma10=0.02, distance_from_ma20=0.05,
        intraday_return=0.01, gap_up_pct=0.0,
        volume=2_000_000, avg_volume_20d=1_500_000,
    )
    d = check_starter_entry(state, ind, cfg)
    assert d is not None
    assert d.action == ActionType.BUY_STARTER
    assert d.new_state == BotState.STARTER_LONG


def test_pullback_base_entry_from_flat():
    cfg = base_config()
    state = make_state()
    ind = make_indicators(
        adj_close=100.0, ma5=99, ma20=95, ma50=90,
        drawdown_from_20d_high=0.05,
    )
    d = check_pullback_base(state, ind, cfg)
    assert d is not None
    assert d.action == ActionType.BUY_BASE
    assert d.new_state == BotState.BASE_LONG


def test_pullback_base_entry_from_starter_long():
    cfg = base_config()
    state = make_state(state=BotState.STARTER_LONG)
    ind = make_indicators(
        adj_close=100.0, ma5=99, ma20=95, ma50=90,
        drawdown_from_20d_high=0.05,
    )
    d = check_pullback_base(state, ind, cfg)
    assert d is not None
    assert d.action == ActionType.BUY_BASE


def test_breakout_base_uses_prior_highest_high_not_current():
    cfg = base_config()
    state = make_state()
    # prior_highest_high_20d=99 (yesterday's rolling max); today's adj=100 > 99 → break
    ind = make_indicators(
        adj_close=100.0, ma5=99, ma10=98, ma20=95, ma50=90,
        prior_highest_high_20d=99.0,
        volume=3_000_000, avg_volume_20d=1_500_000,
        distance_from_ma10=0.02, intraday_return=0.01,
    )
    d = check_breakout_base(state, ind, cfg)
    assert d is not None

    # If prior_highest_high_20d=101 (no breakout), entry should NOT fire
    ind2 = make_indicators(
        adj_close=100.0, ma5=99, ma10=98, ma20=95, ma50=90,
        prior_highest_high_20d=101.0,
        volume=3_000_000, avg_volume_20d=1_500_000,
        distance_from_ma10=0.02, intraday_return=0.01,
    )
    assert check_breakout_base(state, ind2, cfg) is None


def test_breakout_base_entry_when_conditions_met():
    cfg = base_config()
    state = make_state()
    ind = make_indicators(
        adj_close=110.0, ma5=99, ma10=98, ma20=95, ma50=90,
        prior_highest_high_20d=105.0,
        volume=3_000_000, avg_volume_20d=1_500_000,
        distance_from_ma10=0.05, intraday_return=0.02,
    )
    d = check_breakout_base(state, ind, cfg)
    assert d is not None
    assert d.action == ActionType.BUY_BASE
    assert d.new_state == BotState.BASE_LONG


def test_no_entry_inside_event_window():
    cfg = base_config()
    state = make_state(days_to_event=5)
    ind = make_indicators(
        adj_close=100.0, ma5=99, ma10=98, ma20=95, ma50=90,
        distance_from_ma10=0.02, distance_from_ma20=0.05,
        intraday_return=0.01, gap_up_pct=0.0,
        volume=2_000_000, avg_volume_20d=1_500_000,
    )
    assert check_starter_entry(state, ind, cfg) is None
    # Also blocks pullback / breakout
    ind2 = make_indicators(
        adj_close=100.0, ma5=99, ma20=95, ma50=90, drawdown_from_20d_high=0.05,
    )
    assert check_pullback_base(state, ind2, cfg) is None
