from __future__ import annotations

import math
from typing import Optional

from src.core.models import (
    ActionType, BotConfig, BotState, Decision, IndicatorSnapshot, ThesisState,
)
from src.strategy.reduce import drawdown_from_peak


def _profit_from_avg(state: ThesisState, indicators: IndicatorSnapshot) -> float:
    avg = state.avg_entry_price
    if avg is None or avg <= 0:
        return 0.0
    return (indicators.adj_close - avg) / avg


def _ratchet_map(avg: float) -> dict:
    return {
        "breakeven":         avg,
        "lock_20pct_profit": avg * 1.20,
        "lock_35pct_profit": avg * 1.35,
        "lock_50pct_profit": avg * 1.50,
    }


def check_layered_tp(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """
    Section 14.1. Mutates state.tp_levels_triggered for all levels at/below
    current profit, and raises state.trailing_stop_price per move_stop_to.
    Returns a Decision only for the highest *newly* triggered level.
    """
    if state.current_position_shares <= 0:
        return None
    lp = config.take_profit.get("layered_profit", {})
    if not lp.get("enabled", False):
        return None
    levels = lp.get("levels", [])
    if not levels:
        return None

    profit = _profit_from_avg(state, indicators)
    highest_newly_triggered = -1
    for i, lvl in enumerate(levels):
        if profit >= lvl["trigger_profit_pct_from_avg_entry"]:
            if not state.tp_levels_triggered[i]:
                highest_newly_triggered = i
    if highest_newly_triggered < 0:
        return None

    # Mark all triggered levels (Section 8.4)
    for i, lvl in enumerate(levels):
        if profit >= lvl["trigger_profit_pct_from_avg_entry"]:
            state.tp_levels_triggered[i] = True

    lvl = levels[highest_newly_triggered]
    shares_to_sell = math.floor(lvl["reduce_pct_of_total_position"] * state.current_position_shares)
    if shares_to_sell <= 0:
        return None

    # Trailing-stop ratchet
    avg = state.avg_entry_price or 0.0
    move = lvl.get("move_stop_to")
    if avg > 0 and move in _ratchet_map(avg):
        candidate = _ratchet_map(avg)[move]
        if state.trailing_stop_price is None or candidate > state.trailing_stop_price:
            state.trailing_stop_price = candidate

    return Decision(
        action=ActionType.SELL_TAKE_PROFIT,
        shares=shares_to_sell,
        reason=f"layered_tp_level_{highest_newly_triggered + 1}",
        indicators=indicators,
    )


def check_target_tp(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """Section 14.2."""
    if state.current_position_shares <= 0:
        return None
    if state.target_price_tp_triggered:
        return None
    if state.state not in {
        BotState.STARTER_LONG, BotState.BASE_LONG, BotState.PYRAMID_LONG,
        BotState.REDUCE_MODE, BotState.EVENT_RISK_MODE,
    }:
        return None
    tp_cfg = config.take_profit.get("target_price", {})
    if not tp_cfg.get("enabled", False):
        return None
    target = config.thesis.get("target_price")
    if target is None or indicators.adj_close < target:
        return None

    state.target_price_tp_triggered = True

    runner_cfg = config.take_profit.get("runner_mode", {})
    if runner_cfg.get("enabled", False) and state.base_lot is not None:
        runner_pct = runner_cfg.get("runner_position_pct_of_original_base", 0.25)
        runner_target_shares = math.floor(state.base_lot.shares * runner_pct)
        state.runner_target_shares = runner_target_shares
        state.runner_mode_active = True
        # Tight trailing stop
        tight_pct = runner_cfg.get("trailing_stop_pct", 0.08)
        state.trailing_stop_price = indicators.adj_close * (1 - tight_pct)
        shares_to_sell = state.current_position_shares - runner_target_shares
        if shares_to_sell <= 0:
            shares_to_sell = state.current_position_shares
        return Decision(
            action=ActionType.SELL_TAKE_PROFIT,
            shares=shares_to_sell,
            reason="target_tp_runner_mode",
            new_state=BotState.RUNNER_LONG,
            indicators=indicators,
        )

    reduce_pct = tp_cfg.get("reduce_pct_of_remaining_position", 0.50)
    shares_to_sell = math.floor(reduce_pct * state.current_position_shares)
    if shares_to_sell <= 0:
        return None
    return Decision(
        action=ActionType.SELL_TAKE_PROFIT,
        shares=shares_to_sell,
        reason="target_tp",
        indicators=indicators,
    )


def check_exposure_tp(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """Section 14.3 — hard cap only triggers sell (soft cap blocks adds, handled in add.py)."""
    if state.current_position_shares <= 0:
        return None
    etp = config.take_profit.get("exposure_take_profit", {})
    if not etp.get("enabled", False):
        return None

    starting = config.capital["starting_equity"]
    current_exposure_pct = (state.current_position_shares * indicators.adj_close) / starting
    hard_cap = etp.get("hard_exposure_cap_pct", 1.00)
    reduce_to = etp.get("reduce_to_exposure_pct", 0.70)

    if current_exposure_pct >= hard_cap:
        target_notional = reduce_to * starting
        target_shares = math.floor(target_notional / indicators.adj_close)
        shares_to_sell = state.current_position_shares - target_shares
        if shares_to_sell > 0:
            return Decision(
                action=ActionType.SELL_TAKE_PROFIT,
                shares=shares_to_sell,
                reason="exposure_tp_hard_cap",
                indicators=indicators,
            )
    return None


def check_giveback_tp(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """Section 14.4."""
    if state.current_position_shares <= 0:
        return None
    gb = config.take_profit.get("profit_giveback", {})
    if not gb.get("enabled", False):
        return None

    starting = config.capital["starting_equity"]
    avg = state.avg_entry_price or 0.0
    if avg <= 0:
        return None

    unrealized_pnl = (indicators.adj_close - avg) * state.current_position_shares
    unrealized_pct = unrealized_pnl / starting

    state.peak_unrealized_pnl_pct = max(state.peak_unrealized_pnl_pct, unrealized_pct)
    peak = state.peak_unrealized_pnl_pct
    if peak <= 0:
        return None
    giveback_pct = (peak - unrealized_pct) / peak

    tiers = sorted(
        gb.get("tiers", []),
        key=lambda t: t["min_unrealized_profit_pct_of_equity"],
        reverse=True,
    )
    for tier in tiers:
        if peak >= tier["min_unrealized_profit_pct_of_equity"]:
            if giveback_pct > tier["max_giveback_pct"]:
                total_addons = sum(l.shares for l in state.addon_lots)
                shares_to_sell = total_addons
                if shares_to_sell <= 0 and state.base_lot is not None:
                    shares_to_sell = state.base_lot.shares // 2
                if shares_to_sell <= 0:
                    return None
                return Decision(
                    action=ActionType.SELL_TAKE_PROFIT,
                    shares=shares_to_sell,
                    reason="profit_giveback_tp",
                    indicators=indicators,
                )
            break
    return None


def check_trailing_tp(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """Section 14.5."""
    if state.current_position_shares <= 0:
        return None
    ttp = config.take_profit.get("trailing_take_profit", {})
    if not ttp.get("enabled", False):
        return None

    profit = _profit_from_avg(state, indicators)
    dd = drawdown_from_peak(state, indicators)

    tiers = sorted(
        ttp.get("tiers", []),
        key=lambda t: t["min_profit_pct_from_avg_entry"],
        reverse=True,
    )
    for tier in tiers:
        if profit >= tier["min_profit_pct_from_avg_entry"]:
            if dd >= tier["trailing_pct"]:
                action = tier["action"]
                total_addons = sum(l.shares for l in state.addon_lots)
                if action == "reduce_addons_50pct":
                    shares_to_sell = math.floor(total_addons * 0.50)
                elif action == "reduce_all_addons":
                    shares_to_sell = total_addons
                elif action == "reduce_to_core":
                    base_target = (
                        math.floor(state.base_lot.shares * 0.25)
                        if state.base_lot is not None else 0
                    )
                    base_reduce_amt = (
                        state.base_lot.shares - base_target if state.base_lot is not None else 0
                    )
                    shares_to_sell = total_addons + max(0, base_reduce_amt)
                else:
                    shares_to_sell = 0
                if shares_to_sell <= 0:
                    return None
                return Decision(
                    action=ActionType.SELL_TAKE_PROFIT,
                    shares=shares_to_sell,
                    reason=f"trailing_tp_{action}",
                    indicators=indicators,
                )
            break
    return None


def check_take_profit(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """
    Combined TP rule check.  Order: target_price > layered > exposure > giveback > trailing.
    First match wins (Section 8.1 Step 10).
    """
    for fn in (
        check_target_tp,
        check_layered_tp,
        check_exposure_tp,
        check_giveback_tp,
        check_trailing_tp,
    ):
        d = fn(state, indicators, config)
        if d is not None:
            return d
    return None
