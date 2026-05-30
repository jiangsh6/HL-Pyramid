"""
Data and state validators (Section 6.2).

validate_data(indicators, state, config) -> List[ValidationIssue]

Two severity levels:
  HALT — set state = HALTED, halt_reason, require manual reset_state.py --confirm
  WARN — skip trading this cycle; do NOT halt

This module is PURE: it never mutates state, never does I/O.
The caller is responsible for applying HALT issues to state.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List

from src.core.models import BotConfig, IndicatorSnapshot, ThesisState

HALT = "HALT"
WARN = "WARN"

# Indicator fields checked for NaN (Section 6.2).
_NAN_CHECKED_FIELDS = (
    "adj_close",
    "ma5",
    "ma10",
    "ma20",
    "ma50",
    "atr14",
    "avg_volume_20d",
    "prior_highest_high_20d",
)


@dataclass(frozen=True)
class ValidationIssue:
    severity: str   # HALT or WARN
    reason:   str


def validate_data(
    indicators: IndicatorSnapshot,
    state: ThesisState,
    config: BotConfig,
) -> List[ValidationIssue]:
    """
    Run all data-quality and state-consistency checks.

    Returns a (possibly empty) list of ValidationIssue objects ordered by
    decreasing severity.  Empty list means everything is clean.

    IMPORTANT: This function never mutates state.  The caller must apply
    HALT-severity issues by setting state.state = HALTED and
    state.halt_reason = issue.reason.
    """
    issues: List[ValidationIssue] = []

    # ── HALT: any key indicator NaN on the latest bar ─────────────────────
    for field_name in _NAN_CHECKED_FIELDS:
        val = getattr(indicators, field_name, None)
        if isinstance(val, float) and math.isnan(val):
            issues.append(ValidationIssue(
                severity=HALT,
                reason=f"nan_indicator: {field_name}",
            ))
            break   # one NaN halt per cycle is sufficient

    # ── HALT: adj_close zero or negative (distinct from NaN) ─────────────
    adj = indicators.adj_close
    adj_is_nan = isinstance(adj, float) and math.isnan(adj)
    if not adj_is_nan and adj <= 0:
        issues.append(ValidationIssue(
            severity=HALT,
            reason=f"adj_close_invalid: value={adj}",
        ))

    # ── HALT: state internal consistency ─────────────────────────────────
    expected_shares = (
        (state.base_lot.shares if state.base_lot is not None else 0)
        + sum(lot.shares for lot in state.addon_lots)
    )
    if state.current_position_shares != expected_shares:
        issues.append(ValidationIssue(
            severity=HALT,
            reason=(
                f"state_consistency_error: "
                f"current_position_shares={state.current_position_shares} "
                f"but base_lot+addons={expected_shares}"
            ),
        ))

    # ── WARN: liquidation price above initial stop (HL Phase 3) ─────────
    if (state.liquidation_price is not None
            and state.initial_stop_price is not None
            and state.liquidation_price > state.initial_stop_price):
        issues.append(ValidationIssue(
            severity=WARN,
            reason=(
                f"liquidation_price_above_stop: "
                f"liq={state.liquidation_price} > initial_stop={state.initial_stop_price}. "
                f"Hard stop must be raised to avoid forced liquidation."
            ),
        ))

    if state.last_funding_rate > 0.0005:
        issues.append(ValidationIssue(
            severity=WARN,
            reason=(
                f"High funding rate: {state.last_funding_rate:.4%}/hr. "
                f"Holding cost is elevated."
            ),
        ))

    # ── WARN: average daily dollar volume below minimum ───────────────────
    min_dollar_vol = float(
        config.risk["no_trade_conditions"].get("min_avg_daily_dollar_volume", 0)
    )
    if min_dollar_vol > 0 and not adj_is_nan and adj > 0:
        avg_dollar_vol = indicators.avg_volume_20d * adj
        if avg_dollar_vol < min_dollar_vol:
            issues.append(ValidationIssue(
                severity=WARN,
                reason=(
                    f"low_daily_dollar_volume: "
                    f"avg=${avg_dollar_vol:,.0f} < min=${min_dollar_vol:,.0f}"
                ),
            ))

    return issues


def has_halt(issues: List[ValidationIssue]) -> bool:
    """True if any issue requires halting."""
    return any(i.severity == HALT for i in issues)


def has_warn(issues: List[ValidationIssue]) -> bool:
    """True if any issue is a warning (no trade but no halt)."""
    return any(i.severity == WARN for i in issues)
