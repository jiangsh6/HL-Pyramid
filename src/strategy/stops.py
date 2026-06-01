from __future__ import annotations

from typing import Optional

from src.core.models import EPSILON, BotConfig, IndicatorSnapshot, ThesisState


def calc_initial_stop(entry_price: float, atr14: float, config: BotConfig) -> float:
    """
    initial_stop = min(pct_stop, atr_stop)  — lower = wider stop for long.
    Section 11.
    """
    stop_pct = config.risk["initial_stop"]["stop_pct"]
    atr_mult = config.risk["initial_stop"]["atr_multiplier"]
    pct_stop = entry_price * (1 - stop_pct)
    atr_stop = entry_price - atr_mult * atr14
    return min(pct_stop, atr_stop)


def hard_stop_price(state: ThesisState, config: BotConfig) -> float | None:
    """
    Minimal Bundle 3A hard stop:
    avg_entry_price * (1 - risk.hard_stop.stop_loss_pct).

    If the new block is absent, preserve the existing initial_stop_price path.
    """
    if state.avg_entry_price is None or state.avg_entry_price <= 0:
        return state.initial_stop_price
    hard_stop_cfg = config.risk.get("hard_stop", {})
    stop_loss_pct = hard_stop_cfg.get("stop_loss_pct")
    if stop_loss_pct is None:
        return state.initial_stop_price
    stop_loss_pct = float(stop_loss_pct)
    if stop_loss_pct <= 0:
        return state.initial_stop_price
    return state.avg_entry_price * (1 - stop_loss_pct)


def _determine_trailing_pct(profit_from_avg: float, config: BotConfig) -> float:
    ts_cfg = config.risk["trailing_stop"]
    trailing_pct = ts_cfg["default_trailing_pct"]
    tiers = sorted(
        ts_cfg.get("profit_tiers", []),
        key=lambda t: t["min_profit_pct"],
        reverse=True,
    )
    for tier in tiers:
        if profit_from_avg >= tier["min_profit_pct"]:
            return tier["trailing_pct"]
    return trailing_pct


def update_trailing_stop(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> None:
    """
    Section 16.2 / Decision-engine Step 4.
    Mutates state.highest_price_since_entry and state.trailing_stop_price.
    Trailing stop is never lowered.
    """
    if state.current_position_qty <= EPSILON:
        return

    state.highest_price_since_entry = max(
        state.highest_price_since_entry or 0.0,
        indicators.high,
    )
    avg = state.avg_entry_price or indicators.adj_close
    profit_from_avg = (indicators.adj_close - avg) / avg if avg > 0 else 0.0
    trailing_pct = _determine_trailing_pct(profit_from_avg, config)
    candidate = state.highest_price_since_entry * (1 - trailing_pct)
    state.trailing_stop_price = max(state.trailing_stop_price or 0.0, candidate)


def check_stop_triggers(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[str]:
    """
    Returns "hard_stop", "trailing_stop", or None.
    Uses adj_close (not intraday low) per Section 16.2.
    """
    if state.current_position_qty <= EPSILON:
        return None
    trigger_price = hard_stop_price(state, config)
    if trigger_price is not None and indicators.adj_close <= trigger_price:
        return "hard_stop"
    if state.trailing_stop_price is not None and indicators.adj_close <= state.trailing_stop_price:
        return "trailing_stop"
    return None


def check_intraday_drawdown(
    state: ThesisState,
    current_price: float,
    config: BotConfig,
) -> bool:
    """
    Section 8.2 / 16.3 — intraday-only check.
    Returns True if intraday drawdown breaches the halt threshold.
    """
    if state.current_position_qty <= EPSILON:
        return False
    avg = state.avg_entry_price or 0.0
    if avg <= 0:
        return False
    starting = config.capital["starting_equity"]
    intraday_pnl = (current_price - avg) * state.current_position_qty
    max_dd_pct = config.risk["max_loss"]["max_intraday_drawdown_pct_of_equity"]
    return (intraday_pnl / starting) <= -max_dd_pct
