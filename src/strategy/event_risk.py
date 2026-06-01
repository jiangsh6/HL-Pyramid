from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np

from src.core.models import (
    EPSILON, ActionType, BotConfig, BotState, Decision, IndicatorSnapshot, ThesisState,
)


def calc_trading_days_to_event(
    today: date,
    event_date: date,
    use_calendar_days: bool = False,
) -> int:
    """
    Day count between today and event_date (excluding today).
    Returns negative if event already passed.
    When use_calendar_days=True, counts all calendar days instead of business days.
    """
    if use_calendar_days:
        return (event_date - today).days
    if event_date >= today:
        return int(np.busday_count(today, event_date))
    return -int(np.busday_count(event_date, today))


def event_date_from_config(config: BotConfig) -> Optional[date]:
    s = config.thesis.get("event_date")
    if not s:
        return None
    return date.fromisoformat(str(s))


def update_event_risk_mode(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> None:
    """
    Section 15A / Step 5. Mode transitions ONLY — no orders generated.
    Mutates state.state, state.prior_state, state.days_to_event.
    """
    if not config.event_risk.get("enabled", False):
        return
    event_date = event_date_from_config(config)
    if event_date is None:
        return

    today = indicators.date
    use_calendar = config.event_risk.get("use_calendar_days", False)
    days_to_event = calc_trading_days_to_event(today, event_date, use_calendar)
    state.days_to_event = days_to_event
    threshold = config.event_risk.get("stop_adding_trading_days_before", 10)
    cooldown = config.event_risk["post_event"].get("cooldown_trading_days", 1)

    eligible = {
        BotState.STARTER_LONG, BotState.BASE_LONG,
        BotState.PYRAMID_LONG, BotState.REDUCE_MODE,
    }
    if (state.state in eligible
            and 0 <= days_to_event <= threshold):
        state.prior_state = state.state
        state.state = BotState.EVENT_RISK_MODE
        return

    if state.state == BotState.EVENT_RISK_MODE and days_to_event < -cooldown:
        if state.prior_state is not None:
            state.state = state.prior_state
        elif state.current_position_qty > EPSILON:
            state.state = BotState.BASE_LONG
        else:
            state.state = BotState.FLAT
        state.prior_state = None
        if config.event_risk["post_event"].get("reset_add_count", False):
            state.add_count = 0
            state.protect_profit_mode = False


def check_event_derisking(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """
    Section 15B / Step 9 — T-5 / T-2 / T-1 de-risking orders.
    Caller must skip this step if a hard/trailing stop already fired.
    """
    if not config.event_risk.get("enabled", False):
        return None
    if state.current_position_qty <= EPSILON:
        return None
    event_date = event_date_from_config(config)
    if event_date is None:
        return None

    today = indicators.date
    use_calendar = config.event_risk.get("use_calendar_days", False)
    days_to_event = calc_trading_days_to_event(today, event_date, use_calendar)
    if days_to_event < 0:
        return None

    starting = config.capital["starting_equity"]
    current_exposure_pct = (state.current_position_qty * indicators.adj_close) / starting

    # T-1 force flat
    force_flat = config.event_risk.get("force_flat_before_event", False)
    force_flat_t = config.event_risk.get("force_flat_trading_days_before", 1)
    if force_flat and 0 <= days_to_event <= force_flat_t:
        return Decision(
            action=ActionType.SELL_EVENT_DERISKING,
            qty=state.current_position_qty,
            reason=f"event_force_flat_T-{days_to_event}",
            new_state=BotState.EXITED,
            indicators=indicators,
        )

    # T-2 reduce to core
    t2 = config.event_risk.get("reduce_to_core_trading_days_before", 2)
    core_pct = config.event_risk.get("core_exposure_before_event_pct", 0.20)
    if 0 < days_to_event <= t2 and current_exposure_pct > core_pct:
        target_notional = core_pct * starting
        target_qty = target_notional / indicators.adj_close
        qty_to_sell = state.current_position_qty - target_qty
        if qty_to_sell > EPSILON:
            return Decision(
                action=ActionType.SELL_EVENT_DERISKING,
                qty=qty_to_sell,
                reason=f"event_t{days_to_event}_reduce_to_core_{int(core_pct*100)}pct",
                indicators=indicators,
            )

    # T-5 reduce to max
    t5 = config.event_risk.get("reduce_to_max_exposure_trading_days_before", 5)
    max_pct = config.event_risk.get("max_exposure_before_event_pct", 0.50)
    if 0 < days_to_event <= t5 and current_exposure_pct > max_pct:
        target_notional = max_pct * starting
        target_qty = target_notional / indicators.adj_close
        qty_to_sell = state.current_position_qty - target_qty
        if qty_to_sell > EPSILON:
            return Decision(
                action=ActionType.SELL_EVENT_DERISKING,
                qty=qty_to_sell,
                reason=f"event_t{days_to_event}_reduce_to_{int(max_pct*100)}pct",
                indicators=indicators,
            )

    return None
