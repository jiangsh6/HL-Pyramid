from __future__ import annotations

from typing import Optional

from src.core.models import (
    EPSILON, ActionType, BotConfig, BotState, Decision, IndicatorSnapshot, ThesisState,
)


def check_add_conditions(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """
    Section 12. Returns Decision (BUY_ADDON) if all conditions met,
    Decision(NO_ACTION, blockers=[...]) if blocked, or None if state ineligible.
    """
    add_cfg = config.add

    if state.state not in {BotState.BASE_LONG, BotState.RUNNER_LONG, BotState.PYRAMID_LONG}:
        return None
    if not add_cfg.get("enabled", False):
        return None

    blockers = []

    if state.protect_profit_mode:
        blockers.append("protect_profit_mode")

    max_add = add_cfg.get("max_add_count", 4)
    if state.add_count >= max_add:
        blockers.append("max_add_count_reached")

    min_days_after_entry = add_cfg.get("min_days_after_entry_before_first_add", 2)
    min_days_between = add_cfg.get("min_days_between_adds", 1)
    today = indicators.date
    if state.entry_date is not None:
        days_since_entry = (today - state.entry_date).days
        if days_since_entry < min_days_after_entry:
            blockers.append("entry_cooldown")
    if state.addon_lots:
        last_lot_date = state.addon_lots[-1].entry_date
        days_since_last = (today - last_lot_date).days
        if days_since_last < min_days_between:
            blockers.append("between_adds_cooldown")

    avg = state.avg_entry_price
    if avg is None or avg <= 0:
        blockers.append("no_avg_entry_price")
    else:
        unrealized_pct = (indicators.adj_close - avg) / avg
        min_profit = add_cfg.get("min_unrealized_profit_before_first_add_pct", 0.04)
        if unrealized_pct < min_profit:
            blockers.append("profit_below_threshold")
        if unrealized_pct <= 0:
            if "profit_below_threshold" not in blockers:
                blockers.append("position_losing")

    if state.last_add_price is None:
        blockers.append("no_last_add_price")
    else:
        trigger_pct = add_cfg.get("add_trigger_pct", 0.05)
        if indicators.adj_close < state.last_add_price * (1 + trigger_pct):
            blockers.append("price_step_not_met")

    if add_cfg.get("require_close_above_ma10", True):
        if indicators.adj_close <= indicators.ma10:
            blockers.append("close_below_ma10")
    if add_cfg.get("require_ma10_above_ma20", True):
        if indicators.ma10 <= indicators.ma20:
            blockers.append("ma10_below_ma20")

    if indicators.intraday_return > add_cfg.get("forbid_add_if_intraday_gain_above_pct", 0.10):
        blockers.append("intraday_gain_exceeded")
    if indicators.distance_from_ma10 > add_cfg.get("forbid_add_if_distance_above_ma10_pct", 0.12):
        blockers.append("distance_ma10_exceeded")
    if indicators.distance_from_ma20 > add_cfg.get("forbid_add_if_distance_above_ma20_pct", 0.20):
        blockers.append("distance_ma20_exceeded")

    # Event window
    if state.days_to_event is not None:
        stop_thresh = add_cfg.get("stop_adding_if_event_within_trading_days", 10)
        if 0 <= state.days_to_event <= stop_thresh:
            blockers.append("event_window")

    # Soft exposure cap (Section 14.3 — block adds when at/above soft cap)
    etp = config.take_profit.get("exposure_take_profit", {})
    if etp.get("enabled", False) and state.current_position_qty > EPSILON:
        starting = config.capital["starting_equity"]
        current_exposure_pct = (state.current_position_qty * indicators.adj_close) / starting
        soft = etp.get("soft_exposure_cap_pct", 0.80)
        hard = etp.get("hard_exposure_cap_pct", 1.00)
        if soft <= current_exposure_pct < hard:
            blockers.append("soft_exposure_cap")

    if blockers:
        return Decision(
            action=ActionType.NO_ACTION,
            reason="add_blocked",
            blockers=blockers,
            indicators=indicators,
        )

    new_state = BotState.PYRAMID_LONG if state.state != BotState.PYRAMID_LONG else None
    return Decision(
        action=ActionType.BUY_ADDON,
        qty=0,
        reason="add_conditions_met",
        new_state=new_state,
        indicators=indicators,
    )
