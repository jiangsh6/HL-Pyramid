from __future__ import annotations

from typing import Optional

from src.core.models import (
    ActionType, BotConfig, BotState, Decision, IndicatorSnapshot, ThesisState,
)


def _event_blocks_entry(state: ThesisState, config: BotConfig) -> bool:
    days = state.days_to_event
    if days is None:
        return False
    thresh = config.event_risk.get("stop_new_entry_trading_days_before", 10)
    return 0 <= days <= thresh


def check_starter_entry(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """Section 9.1."""
    if state.state != BotState.FLAT:
        return None
    s = config.entry.get("starter", {})
    if not s.get("enabled", False):
        return None
    if s.get("require_thesis_enabled", True) and not state.thesis_enabled:
        return None
    if config.thesis.get("enabled", True) is False:
        return None

    if s.get("require_close_above_ma20", True):
        if not (indicators.adj_close > indicators.ma20):
            return None
    if indicators.distance_from_ma10 > s.get("max_distance_above_ma10_pct", 0.10):
        return None
    if indicators.distance_from_ma20 > s.get("max_distance_above_ma20_pct", 0.15):
        return None
    if indicators.intraday_return > s.get("max_intraday_gain_pct", 0.08):
        return None
    if indicators.gap_up_pct > s.get("max_gap_up_pct", 0.06):
        return None

    min_vol = s.get("min_volume_vs_20d_avg", 0.80)
    if not (indicators.volume >= min_vol * indicators.avg_volume_20d):
        return None

    if _event_blocks_entry(state, config):
        return None

    return Decision(
        action=ActionType.BUY_STARTER,
        qty=0,
        reason="starter_entry_conditions_met",
        new_state=BotState.STARTER_LONG,
        indicators=indicators,
    )


def check_pullback_base(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """Section 9.2."""
    if state.state not in {BotState.FLAT, BotState.STARTER_LONG}:
        return None
    pb = config.entry.get("pullback_base", {})
    if not pb.get("enabled", False):
        return None

    if pb.get("require_close_above_ma20", True):
        if not (indicators.adj_close > indicators.ma20):
            return None
    if pb.get("require_ma20_above_ma50", True):
        if not (indicators.ma20 > indicators.ma50):
            return None

    min_dd = pb.get("min_drawdown_from_20d_high_pct", 0.03)
    max_dd = pb.get("max_drawdown_from_20d_high_pct", 0.08)
    if not (min_dd <= indicators.drawdown_from_20d_high <= max_dd):
        return None

    if pb.get("require_reclaim_ma5", True):
        if not (indicators.adj_close > indicators.ma5):
            return None

    if _event_blocks_entry(state, config):
        return None

    return Decision(
        action=ActionType.BUY_BASE,
        qty=0,
        reason="pullback_base_conditions_met",
        new_state=BotState.BASE_LONG,
        indicators=indicators,
    )


def check_breakout_base(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """Section 9.3."""
    if state.state not in {BotState.FLAT, BotState.STARTER_LONG}:
        return None
    bo = config.entry.get("breakout_base", {})
    if not bo.get("enabled", False):
        return None

    if bo.get("require_close_above_20d_high", True):
        if not (indicators.adj_close > indicators.prior_highest_high_20d):
            return None

    min_vol = bo.get("min_volume_vs_20d_avg", 1.50)
    if not (indicators.volume > min_vol * indicators.avg_volume_20d):
        return None

    if bo.get("require_close_above_ma10", True):
        if not (indicators.adj_close > indicators.ma10):
            return None
    if bo.get("require_ma10_above_ma20", True):
        if not (indicators.ma10 > indicators.ma20):
            return None
    if bo.get("require_ma20_above_ma50", True):
        if not (indicators.ma20 > indicators.ma50):
            return None

    if indicators.distance_from_ma10 > bo.get("max_distance_above_ma10_pct", 0.08):
        return None
    if indicators.intraday_return > bo.get("max_intraday_gain_pct", 0.10):
        return None

    if _event_blocks_entry(state, config):
        return None

    return Decision(
        action=ActionType.BUY_BASE,
        qty=0,
        reason="breakout_base_conditions_met",
        new_state=BotState.BASE_LONG,
        indicators=indicators,
    )


def check_entry(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """Try entries in order — starter (FLAT only), pullback, breakout."""
    if state.state == BotState.FLAT:
        d = check_starter_entry(state, indicators, config)
        if d:
            return d
    d = check_pullback_base(state, indicators, config)
    if d:
        return d
    d = check_breakout_base(state, indicators, config)
    if d:
        return d
    return None
