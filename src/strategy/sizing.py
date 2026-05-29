from __future__ import annotations

import math
from typing import Optional, Tuple

from src.core.models import BotConfig, ThesisState
from src.strategy.stops import calc_initial_stop


def available_capital(config: BotConfig) -> float:
    starting = config.capital["starting_equity"]
    reserve = config.capital["reserve_cash_pct"]
    return starting * (1 - reserve)


def calc_entry_size(
    state: ThesisState,
    config: BotConfig,
    entry_price: float,
    atr14: float,
    intended_exposure_pct: float,
) -> Tuple[int, Optional[str]]:
    """
    Section 10.1.
    Returns (final_shares, blocker_reason). blocker_reason is None on success.
    """
    starting = config.capital["starting_equity"]
    risk_pct = config.capital["max_total_capital_at_risk_pct"]
    max_risk_budget = starting * risk_pct

    stop_price = calc_initial_stop(entry_price, atr14, config)
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return (0, "stop_above_entry_price")

    shares_by_risk = math.floor(max_risk_budget / risk_per_share)
    target_notional = intended_exposure_pct * available_capital(config)
    shares_by_exposure = math.floor(target_notional / entry_price)
    final_shares = min(shares_by_risk, shares_by_exposure)

    if final_shares <= 0:
        return (0, "position_size_zero")
    return (final_shares, None)


def calc_addon_size(
    state: ThesisState,
    config: BotConfig,
    add_price: float,
) -> Tuple[int, Optional[str]]:
    """
    Section 10.2. Uses REMAINING thesis risk budget.
    Returns (final_shares, blocker_reason).
    """
    starting = config.capital["starting_equity"]
    risk_pct = config.capital["max_total_capital_at_risk_pct"]
    max_risk_budget = starting * risk_pct

    trailing_stop = state.trailing_stop_price
    if trailing_stop is None:
        return (0, "trailing_stop_unset")

    all_lots = ([state.base_lot] if state.base_lot is not None else []) + list(state.addon_lots)
    current_open_risk = sum(
        (lot.entry_price - trailing_stop) * lot.shares for lot in all_lots
    )
    remaining = max(0.0, max_risk_budget - current_open_risk)
    if remaining <= 0:
        return (0, "thesis_risk_budget_exhausted")

    incremental = add_price - trailing_stop
    if incremental <= 0:
        return (0, "stop_above_entry_price")

    shares_by_risk = math.floor(remaining / incremental)
    add_sizes_pct = config.add["add_sizes_pct"]
    if state.add_count >= len(add_sizes_pct):
        return (0, "add_count_out_of_range")
    target_notional = add_sizes_pct[state.add_count] * available_capital(config)
    shares_by_exposure = math.floor(target_notional / add_price)
    final_shares = min(shares_by_risk, shares_by_exposure)

    if final_shares <= 0:
        return (0, "position_size_zero")
    return (final_shares, None)
