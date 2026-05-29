"""
Pre-trade risk gate (Section 26).

check_order_allowed(state, decision, config, indicators?) -> (bool, str)

Pure function — no state mutation, no I/O, no paper_broker calls.
Called by the caller before every paper_broker.execute() invocation.

Blocks orders when:
  - state = HALTED  (Section 26 rule 8)
  - state = EXITED  (Section 4.2 — EXITED requires manual reset)
  - daily PnL loss >= max_daily_loss_pct_of_equity  (Section 16.3)
  - proposed buy would push exposure above max_symbol_exposure_pct  (Section 26 rule 5)
  - NaN indicators detected  (belt-and-suspenders, Section 26 rule 3)

Returns (True, "ok") when the order is allowed.
Returns (False, reason_string) when blocked.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

from src.core.models import (
    ActionType, BotConfig, BotState, Decision, IndicatorSnapshot, ThesisState,
)

_BUY_ACTIONS = frozenset({
    ActionType.BUY_STARTER,
    ActionType.BUY_BASE,
    ActionType.BUY_ADDON,
})

_NAN_CHECKED_FIELDS = ("adj_close", "ma5", "ma10", "ma20", "ma50", "atr14")


def check_order_allowed(
    state: ThesisState,
    decision: Decision,
    config: BotConfig,
    indicators: Optional[IndicatorSnapshot] = None,
) -> Tuple[bool, str]:
    """
    Pre-trade gate.

    Parameters
    ----------
    state : ThesisState
        Current bot state (read-only — this function never mutates it).
    decision : Decision
        Proposed order from the decision engine.
    config : BotConfig
        Loaded and validated bot configuration.
    indicators : IndicatorSnapshot, optional
        Latest bar indicators (used for exposure and NaN checks).

    Returns
    -------
    (allowed, reason) : (bool, str)
        allowed=True  means the order may proceed.
        allowed=False means the caller must suppress the order; reason explains why.
    """
    # ── HALTED: no orders ever — only manual reset clears this ─────────────
    if state.state == BotState.HALTED or state.halted:
        return False, "state_is_halted"

    # ── EXITED: no new orders; position is fully closed ────────────────────
    if state.state == BotState.EXITED:
        return False, "state_is_exited"

    # ── Daily PnL loss limit ───────────────────────────────────────────────
    starting = config.capital["starting_equity"]
    max_daily_loss = config.risk["max_loss"]["max_daily_loss_pct_of_equity"]
    if starting > 0 and (state.realized_pnl / starting) <= -max_daily_loss:
        return False, "daily_loss_limit_hit"

    # ── NaN indicators (belt-and-suspenders) ──────────────────────────────
    if indicators is not None:
        for field_name in _NAN_CHECKED_FIELDS:
            val = getattr(indicators, field_name, None)
            if isinstance(val, float) and math.isnan(val):
                return False, f"nan_indicator: {field_name}"

    # ── Exposure cap for buy orders ────────────────────────────────────────
    if (decision.action in _BUY_ACTIONS
            and decision.shares > 0
            and indicators is not None
            and indicators.adj_close > 0):
        max_exposure_pct = float(config.capital.get("max_symbol_exposure_pct", 1.0))
        price = indicators.adj_close
        current_notional  = state.current_position_shares * price
        proposed_notional = decision.shares * price
        new_exposure_pct  = (current_notional + proposed_notional) / starting
        if new_exposure_pct > max_exposure_pct:
            return (
                False,
                (
                    f"exposure_would_exceed_max: "
                    f"proposed={new_exposure_pct:.4f} > max={max_exposure_pct:.4f}"
                ),
            )

    return True, "ok"
