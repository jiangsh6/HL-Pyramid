"""
Paper broker — stateless fill simulator.  Section 18.

execute(decision, fill_ref_price, state, config?) -> Fill

Pure function. Does NOT read or write state.json.  Does NOT modify state.
The caller (run_daily_decision / backtester) is responsible for applying the
returned Fill to state via state_writer.apply_fill().
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Optional

from src.core.models import (
    ActionType, BotConfig, Decision, Fill, ThesisState,
)
from src.execution.utils import compute_realized_pnl_lifo


_BUY_ACTIONS = {
    ActionType.BUY_STARTER, ActionType.BUY_BASE, ActionType.BUY_ADDON,
}
_SELL_ACTIONS = {
    ActionType.SELL_REDUCE_ADDON, ActionType.SELL_REDUCE_BASE,
    ActionType.SELL_TAKE_PROFIT, ActionType.SELL_STOP,
    ActionType.SELL_TRAILING_STOP, ActionType.SELL_EVENT_DERISKING,
    ActionType.EXIT_ALL,
}


def execute(
    decision: Decision,
    fill_ref_price: float,
    state: ThesisState,
    config: Optional[BotConfig] = None,
) -> Fill:
    """
    Compute a Fill at fill_ref_price ± random slippage.
    For sells, realized_pnl uses LIFO cost basis from state lots (read-only).
    Commission = 0.0 (Section 18.2).
    """
    max_slip_bps = 25.0
    if config is not None:
        max_slip_bps = float(config.execution.get("max_slippage_bps", 25))

    slippage = random.uniform(0.0, max_slip_bps / 10000.0)

    if decision.action in _BUY_ACTIONS:
        fill_price = fill_ref_price * (1 + slippage)
        realized_pnl = 0.0
    elif decision.action in _SELL_ACTIONS:
        fill_price = fill_ref_price * (1 - slippage)
        realized_pnl = compute_realized_pnl_lifo(state, decision.qty, fill_price)
    else:
        # NO_ACTION / HALT
        fill_price = fill_ref_price
        realized_pnl = 0.0

    return Fill(
        action=decision.action,
        qty=decision.qty,
        fill_price=fill_price,
        slippage_bps=slippage * 10000.0,
        realized_pnl=realized_pnl,
        commission=0.0,
        timestamp=datetime.now(),
    )


def _compute_realized_pnl(
    state: ThesisState, qty_to_sell: float, fill_price: float
) -> float:
    return compute_realized_pnl_lifo(state, float(qty_to_sell), fill_price)
