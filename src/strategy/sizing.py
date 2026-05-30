from __future__ import annotations

import math
from typing import Optional, Tuple

from src.core.models import BotConfig, ThesisState
from src.strategy.stops import calc_initial_stop


def _size(raw: float, sz_decimals: int) -> float:
    """
    Convert raw notional / price to a contract quantity.

    sz_decimals=0  →  floor to integer (equity / integer-lot compat behavior).
    sz_decimals>0  →  round to sz_decimals places (crypto fractional contracts).

    Using floor for the integer path preserves the original math.floor() behavior
    required by the existing tests (see test_sizing_sz_decimals_0_matches_prior…).
    """
    if sz_decimals == 0:
        return float(math.floor(raw))
    return round(raw, sz_decimals)


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
    sz_decimals: int = 0,
) -> Tuple[float, Optional[str]]:
    """
    Section 10.1.
    Returns (final_contracts, blocker_reason).  blocker_reason is None on success.

    sz_decimals=0  →  integer lots (equity / integer-contract path; same as before).
    sz_decimals>0  →  fractional contracts rounded to sz_decimals places (HL perps).
    """
    starting = config.capital["starting_equity"]
    risk_pct = config.capital["max_total_capital_at_risk_pct"]
    max_risk_budget = starting * risk_pct

    stop_price = calc_initial_stop(entry_price, atr14, config)
    risk_per_contract = entry_price - stop_price
    if risk_per_contract <= 0:
        return (0.0, "stop_above_entry_price")

    contracts_by_risk     = _size(max_risk_budget / risk_per_contract, sz_decimals)
    target_notional       = intended_exposure_pct * available_capital(config)
    contracts_by_exposure = _size(target_notional / entry_price, sz_decimals)
    final_contracts       = min(contracts_by_risk, contracts_by_exposure)

    if final_contracts <= 0.0:
        return (0.0, "position_size_zero")
    return (final_contracts, None)


def calc_addon_size(
    state: ThesisState,
    config: BotConfig,
    add_price: float,
    sz_decimals: int = 0,
) -> Tuple[float, Optional[str]]:
    """
    Section 10.2.  Uses REMAINING thesis risk budget.
    Returns (final_contracts, blocker_reason).
    """
    starting = config.capital["starting_equity"]
    risk_pct = config.capital["max_total_capital_at_risk_pct"]
    max_risk_budget = starting * risk_pct

    trailing_stop = state.trailing_stop_price
    if trailing_stop is None:
        return (0.0, "trailing_stop_unset")

    all_lots = ([state.base_lot] if state.base_lot is not None else []) + list(state.addon_lots)
    current_open_risk = sum(
        (lot.entry_price - trailing_stop) * lot.contracts for lot in all_lots
    )
    remaining = max(0.0, max_risk_budget - current_open_risk)
    if remaining <= 0:
        return (0.0, "thesis_risk_budget_exhausted")

    incremental = add_price - trailing_stop
    if incremental <= 0:
        return (0.0, "stop_above_entry_price")

    contracts_by_risk     = _size(remaining / incremental, sz_decimals)
    add_sizes_pct         = config.add["add_sizes_pct"]
    if state.add_count >= len(add_sizes_pct):
        return (0.0, "add_count_out_of_range")
    target_notional       = add_sizes_pct[state.add_count] * available_capital(config)
    contracts_by_exposure = _size(target_notional / add_price, sz_decimals)
    final_contracts       = min(contracts_by_risk, contracts_by_exposure)

    if final_contracts <= 0.0:
        return (0.0, "position_size_zero")
    return (final_contracts, None)
