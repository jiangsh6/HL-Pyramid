from __future__ import annotations

import math
from typing import List, Optional

from src.core.models import (
    ActionType, BotConfig, BotState, Decision, IndicatorSnapshot, ThesisState,
)
from src.strategy.entry import check_entry
from src.strategy.add import check_add_conditions
from src.strategy.reduce import check_addon_reduce, check_base_reduce
from src.strategy.take_profit import check_take_profit
from src.strategy.stops import update_trailing_stop, check_stop_triggers
from src.strategy.event_risk import update_event_risk_mode, check_event_derisking
from src.strategy.sizing import calc_entry_size, calc_addon_size


def _has_nan(ind: IndicatorSnapshot) -> bool:
    fields = [
        ind.adj_close, ind.ma5, ind.ma10, ind.ma20, ind.ma50, ind.atr14,
        ind.prior_highest_high_20d, ind.avg_volume_20d,
    ]
    return any((isinstance(v, float) and math.isnan(v)) for v in fields)


def _evaluate_protect_profit_mode(
    state: ThesisState, indicators: IndicatorSnapshot, config: BotConfig
) -> None:
    """Section 4.1 — set protect_profit_mode if conditions met.  Never auto-clears."""
    if state.protect_profit_mode:
        return
    max_add = config.add.get("max_add_count", 4)
    if state.add_count >= max_add:
        state.protect_profit_mode = True
        return
    avg = state.avg_entry_price
    if avg and avg > 0 and state.current_position_shares > 0:
        unrealized_pct = (indicators.adj_close - avg) / avg
        levels = config.take_profit.get("layered_profit", {}).get("levels", [])
        if levels:
            lvl1 = levels[0]["trigger_profit_pct_from_avg_entry"]
            if unrealized_pct >= lvl1:
                state.protect_profit_mode = True


def _no_action(reason: str, blockers: Optional[List[str]] = None,
               indicators: Optional[IndicatorSnapshot] = None) -> Decision:
    return Decision(
        action=ActionType.NO_ACTION,
        shares=0,
        reason=reason,
        blockers=blockers or [],
        indicators=indicators,
    )


def run(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Decision:
    """
    Section 8.1 — 13-step decision cycle.
    Mutates state for: trailing stop update, event-risk mode transitions,
    days_to_event, protect_profit_mode, tp_levels_triggered, target_price_tp_triggered,
    peak_unrealized_pnl_pct, runner_target_shares, runner_mode_active.
    Returns a Decision; the caller (Phase 3) executes and applies the Fill.
    """
    # Step 2: indicator validation (Step 1 handled outside)
    if _has_nan(indicators):
        state.state = BotState.HALTED
        state.halted = True
        state.halt_reason = "nan_indicators"
        return Decision(
            action=ActionType.HALT,
            reason="nan_indicators",
            new_state=BotState.HALTED,
            indicators=indicators,
        )

    # Step 3: state consistency
    expected = (state.base_lot.shares if state.base_lot is not None else 0) + sum(
        l.shares for l in state.addon_lots
    )
    if state.current_position_shares != expected:
        state.state = BotState.HALTED
        state.halted = True
        state.halt_reason = "state_consistency_error"
        return Decision(
            action=ActionType.HALT,
            reason="state_consistency_error",
            new_state=BotState.HALTED,
            indicators=indicators,
        )

    # Step 4: trailing stop update BEFORE any trigger check
    update_trailing_stop(state, indicators, config)

    # Step 5: event-risk mode transition (no orders)
    update_event_risk_mode(state, indicators, config)

    # Step 6a: hard stop
    if state.current_position_shares > 0:
        trig = check_stop_triggers(state, indicators, config)
        if trig == "hard_stop":
            return Decision(
                action=ActionType.SELL_STOP,
                shares=state.current_position_shares,
                reason="hard_stop_triggered",
                new_state=BotState.EXITED,
                indicators=indicators,
            )

    # Step 6b: max thesis loss
    starting = config.capital["starting_equity"]
    max_thesis_loss = config.risk["max_loss"]["max_thesis_loss_pct_of_equity"]
    if state.thesis_pnl / starting <= -max_thesis_loss:
        state.halted = True
        state.halt_reason = "max_thesis_loss"
        if state.current_position_shares > 0:
            return Decision(
                action=ActionType.EXIT_ALL,
                shares=state.current_position_shares,
                reason="max_thesis_loss",
                new_state=BotState.HALTED,
                indicators=indicators,
            )
        return Decision(
            action=ActionType.HALT,
            reason="max_thesis_loss",
            new_state=BotState.HALTED,
            indicators=indicators,
        )

    # Step 6c: max daily loss — non-terminal, blocks new orders
    blockers: List[str] = []
    max_daily_loss = config.risk["max_loss"]["max_daily_loss_pct_of_equity"]
    daily_loss_blocks = False
    if state.realized_pnl / starting <= -max_daily_loss:
        daily_loss_blocks = True
        blockers.append("daily_loss_limit")

    # Step 7: trailing stop trigger
    if state.current_position_shares > 0:
        trig = check_stop_triggers(state, indicators, config)
        if trig == "trailing_stop":
            return Decision(
                action=ActionType.SELL_TRAILING_STOP,
                shares=state.current_position_shares,
                reason="trailing_stop_triggered",
                new_state=BotState.EXITED,
                indicators=indicators,
            )

    # Step 8: reduce rules
    if state.current_position_shares > 0:
        d = check_addon_reduce(state, indicators, config)
        if d is not None:
            return d
        d = check_base_reduce(state, indicators, config)
        if d is not None:
            return d

    # Step 9: event de-risking
    if state.current_position_shares > 0:
        d = check_event_derisking(state, indicators, config)
        if d is not None:
            return d

    # Step 10: take profit
    if state.current_position_shares > 0:
        d = check_take_profit(state, indicators, config)
        if d is not None:
            return d

    # Evaluate protect_profit_mode AFTER stops/TP but BEFORE add
    _evaluate_protect_profit_mode(state, indicators, config)

    # Step 11: entry
    if state.state in {BotState.FLAT, BotState.STARTER_LONG} and not daily_loss_blocks:
        d = check_entry(state, indicators, config)
        if d is not None:
            exposure_pct = _entry_exposure_pct(d.reason, config)
            shares, blocker = calc_entry_size(
                state, config, indicators.adj_close, indicators.atr14, exposure_pct
            )
            if blocker is None:
                d.shares = shares
                return d
            blockers.append(blocker)

    # Step 12: add
    if (state.state in {BotState.BASE_LONG, BotState.PYRAMID_LONG}
            and not state.protect_profit_mode
            and not daily_loss_blocks):
        d = check_add_conditions(state, indicators, config)
        if d is not None and d.action == ActionType.BUY_ADDON:
            shares, blocker = calc_addon_size(state, config, indicators.adj_close)
            if blocker is None:
                d.shares = shares
                return d
            blockers.append(blocker)
        elif d is not None and d.blockers:
            blockers.extend(d.blockers)

    return _no_action("no_trigger", blockers=blockers, indicators=indicators)


def _entry_exposure_pct(reason: str, config: BotConfig) -> float:
    if "starter" in reason:
        return config.entry["starter"]["exposure_pct"]
    if "pullback" in reason:
        return config.entry["pullback_base"]["additional_exposure_pct"]
    if "breakout" in reason:
        return config.entry["breakout_base"]["additional_exposure_pct"]
    return config.entry.get("pullback_base", {}).get("additional_exposure_pct", 0.12)
