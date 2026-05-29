"""Formatted daily summary output (Section 20)."""
from __future__ import annotations

from typing import List, Optional

from src.core.models import BotConfig, IndicatorSnapshot, ThesisState


def _safe(val, default=0.0) -> float:
    return val if val is not None else default


def format_summary(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
    action_label: str = "NO ACTION",
    reason: str = "",
    blockers: Optional[List[str]] = None,
) -> str:
    blockers = blockers or []
    equity = config.capital["starting_equity"]
    base_shares = state.base_lot.shares if state.base_lot else 0
    addon_shares = sum(l.shares for l in state.addon_lots)
    notional = state.current_position_shares * indicators.adj_close
    exposure_pct = notional / equity if equity > 0 else 0.0
    avg = _safe(state.avg_entry_price)
    unrealized_pct = (
        (indicators.adj_close - avg) * state.current_position_shares / equity
        if (avg > 0 and equity > 0) else 0.0
    )
    max_add = config.add.get("max_add_count", 4)
    risk_pct = config.capital.get("max_total_capital_at_risk_pct", 0.06)
    max_risk = equity * risk_pct

    open_risk = 0.0
    if state.trailing_stop_price:
        for lot in [state.base_lot] + list(state.addon_lots):
            if lot is None:
                continue
            open_risk += max(0.0, (lot.entry_price - state.trailing_stop_price) * lot.shares)
    remaining_budget = max(0.0, max_risk - open_risk)

    lines = [
        f"{state.symbol} Long Thesis Bot — Daily Summary",
        "=" * 36,
        f"State:               {state.state.value}",
        f"Protect Profit Mode: {state.protect_profit_mode}",
        f"Close:               {indicators.adj_close:.2f}",
        f"Position:            {state.current_position_shares} shares  "
        f"(base: {base_shares} | add-ons: {addon_shares})",
        f"Avg Entry:           {avg:.2f}",
        f"Exposure:            {exposure_pct * 100:.1f}%",
        f"Unrealized PnL:      {unrealized_pct * 100:+.1f}% (account)",
        f"Add Count:           {state.add_count} / {max_add}",
        f"Highest (daily HIGH): {_safe(state.highest_price_since_entry):.2f}",
        f"Trailing Stop:       {_safe(state.trailing_stop_price):.2f}",
        f"Initial Stop:        {_safe(state.initial_stop_price):.2f}",
        f"Days to Event:       {state.days_to_event}",
        f"Runner Mode:         {'Yes' if state.runner_mode_active else 'No'}",
        "",
        "--- Today's Action ---",
        f"Action:              {action_label}",
        f"Reason:              {reason}",
        f"Blockers:            {', '.join(blockers) if blockers else 'none'}",
        "",
        "--- Risk Budget ---",
        f"Max thesis risk:     ${max_risk:,.0f}",
        f"Open risk (all lots): ${open_risk:,.0f}",
        f"Remaining budget:    ${remaining_budget:,.0f}",
    ]
    return "\n".join(lines)
