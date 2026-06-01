"""
Shared execution helpers used by both paper and HL brokers.

compute_realized_pnl_lifo:
    LIFO cost basis for a sell against the current ThesisState lots.
    Read-only — never mutates state.
"""
from __future__ import annotations

from src.core.models import ThesisState


def compute_realized_pnl_lifo(
    state: ThesisState, contracts_to_sell: float, fill_price: float
) -> float:
    """
    LIFO realized PnL for a sell of `contracts_to_sell` at `fill_price`.

    Latest addon lots first, then base if needed. Uses float `contracts`
    on each LotRecord so fractional crypto positions are handled correctly.
    """
    remaining = float(contracts_to_sell)
    realized  = 0.0

    for lot in reversed(state.addon_lots):
        if remaining <= 0:
            break
        sell      = min(lot.qty, remaining)
        realized += (fill_price - lot.entry_price) * sell
        remaining -= sell

    if remaining > 0 and state.base_lot is not None:
        sell      = min(state.base_lot.qty, remaining)
        realized += (fill_price - state.base_lot.entry_price) * sell

    return realized
