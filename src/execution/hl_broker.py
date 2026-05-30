"""
Hyperliquid testnet broker — Phase 4.

execute_hl(decision, state, config, client, wallet_address, private_key, mark_price) -> Fill

Maps Decision → HLOrderRequest → place_order → Fill.
Commission uses Hyperliquid taker fee (0.05%).
SECURITY: private_key is NEVER logged or written to any file.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from src.core.models import ActionType, BotConfig, Decision, Fill, ThesisState
from src.hl.client import HyperliquidClient
from src.hl.order_placer import HLOrderRequest, generate_cloid, place_order

_BUY_ACTIONS = frozenset({
    ActionType.BUY_STARTER,
    ActionType.BUY_BASE,
    ActionType.BUY_ADDON,
})

_SELL_ACTIONS = frozenset({
    ActionType.SELL_REDUCE_ADDON,
    ActionType.SELL_REDUCE_BASE,
    ActionType.SELL_TAKE_PROFIT,
    ActionType.SELL_STOP,
    ActionType.SELL_TRAILING_STOP,
    ActionType.SELL_EVENT_DERISKING,
    ActionType.EXIT_ALL,
})

HL_TAKER_FEE = 0.0005   # 0.05% Hyperliquid taker fee


class HLExecutionError(Exception):
    """Raised when Hyperliquid order placement fails."""


def execute_hl(
    decision: Decision,
    state: ThesisState,
    config: BotConfig,
    client: HyperliquidClient,
    wallet_address: str,
    private_key: str,
    mark_price: float,
) -> Fill:
    """
    Execute a Decision on the Hyperliquid testnet.

    Parameters
    ----------
    decision     : Decision from decision_engine.run()
    state        : Current ThesisState (read-only here)
    config       : Loaded BotConfig
    client       : HyperliquidClient (testnet)
    wallet_address : HL wallet address (0x...)
    private_key  : From HL_PRIVATE_KEY env var — NEVER logged
    mark_price   : Current mark price used to compute limit_px

    Returns
    -------
    Fill
        commission = filled_sz * fill_price * HL_TAKER_FEE (0.05%)

    Raises
    ------
    HLExecutionError
        If the exchange returns an error status or the action is unsupported.
    """
    is_buy  = decision.action in _BUY_ACTIONS
    is_sell = decision.action in _SELL_ACTIONS

    if not is_buy and not is_sell:
        raise HLExecutionError(
            f"Unsupported action for HL execution: {decision.action}"
        )

    offset_bps = float(config.execution.get("limit_offset_bps", 10))
    offset = offset_bps / 10_000.0

    limit_px = mark_price * (1 + offset) if is_buy else mark_price * (1 - offset)

    coin: str = config.hl["coin"] if config.hl else config.symbol.get("ticker", "BTC")
    sz: float = float(decision.contracts) if decision.contracts else float(decision.shares)

    cloid = generate_cloid()

    order_request = HLOrderRequest(
        coin=coin,
        is_buy=is_buy,
        sz=sz,
        limit_px=limit_px,
        order_type={"limit": {"tif": "Gtc"}},
        reduce_only=is_sell,
        cloid=cloid,
    )

    response = place_order(order_request, wallet_address, private_key, client)

    if response.status == "err":
        raise HLExecutionError(response.error or "unknown_hl_error")

    fill_price   = response.avg_fill_px if response.avg_fill_px else limit_px
    filled_sz    = response.filled_sz   if response.filled_sz > 0 else sz
    slippage_bps = abs(fill_price - mark_price) / mark_price * 10_000 if mark_price > 0 else 0.0
    commission   = filled_sz * fill_price * HL_TAKER_FEE

    return Fill(
        action=decision.action,
        contracts=filled_sz,
        fill_price=fill_price,
        slippage_bps=slippage_bps,
        realized_pnl=0.0,   # computed by state_writer in Phase 5
        commission=commission,
        timestamp=datetime.now(),
    )
