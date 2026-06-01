"""
Hyperliquid testnet broker — Phase 4.

execute_hl(decision, state, config, client, wallet_address, private_key, mark_price) -> Fill

Maps Decision → HLOrderRequest → place_order → Fill.
Commission uses Hyperliquid taker fee (0.05%).
SECURITY: private_key is NEVER logged or written to any file.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from src.core.models import (
    ActionType,
    BotConfig,
    Decision,
    OrderResult,
    OrderResultStatus,
    ThesisState,
)
from src.execution.utils import compute_realized_pnl_lifo
from src.hl.client import HyperliquidClient
from src.hl.order_placer import HLOrderRequest, generate_cloid, place_order
from src.hl.pricing import compute_oracle_safe_entry_price, compute_oracle_safe_exit_price

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
_log = logging.getLogger(__name__)
_EMERGENCY_EXIT_ACTIONS = frozenset({
    ActionType.SELL_STOP,
    ActionType.SELL_TRAILING_STOP,
    ActionType.EXIT_ALL,
})


def _tif_to_wire(value: str | None) -> str:
    tif = (value or "gtc").strip().lower()
    if tif == "ioc":
        return "Ioc"
    return "Gtc"


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
) -> OrderResult:
    """
    Execute a Decision on the Hyperliquid testnet.

    Parameters
    ----------
    decision     : Decision from decision_engine.run()
    state        : Current ThesisState (read-only here)
    config       : Loaded BotConfig
    client       : HyperliquidClient (testnet)
    wallet_address : HL wallet address (0x...)
    private_key  : From the configured network-specific agent key env var — NEVER logged
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
        return OrderResult(
            status=OrderResultStatus.EXECUTION_ERROR,
            action=decision.action,
            side="buy" if is_buy else "sell",
            reduce_only=is_sell,
            submitted_qty=float(decision.qty),
            raw_status_sanitized="unsupported_action",
            exchange_error_sanitized=f"Unsupported action for HL execution: {decision.action}",
            state_before=state.state.value,
            intended_state_after=decision.new_state.value if decision.new_state is not None else None,
            applied_state_after=state.state.value,
            created_at=datetime.now(),
        )

    offset_bps = float(config.execution.get("limit_offset_bps", 10))
    offset = offset_bps / 10_000.0

    coin: str = config.hl["coin"] if config.hl else config.symbol.get("ticker", "BTC")
    sz: float = float(decision.qty)
    sz_decimals = int((config.hl or {}).get("sz_decimals", getattr(state, "sz_decimals", 0)) or 0)
    min_size = float((config.hl or {}).get("min_size", 0.0) or 0.0)
    execution_cfg = config.execution or {}
    order_tif = _tif_to_wire(execution_cfg.get("time_in_force"))

    if is_sell and decision.action in _EMERGENCY_EXIT_ACTIONS:
        oracle_price = getattr(state, "oracle_price", None)
        exit_aggr_bps = float(
            execution_cfg.get(
                "exit_price_aggressiveness_bps",
                (config.hl or {}).get("exit_price_aggressiveness_bps", 2),
            )
        )
        max_oracle_bps = float(
            execution_cfg.get(
                "max_oracle_deviation_bps",
                (config.hl or {}).get("max_oracle_deviation_bps", 5),
            )
        )
        price_info = compute_oracle_safe_exit_price(
            "sell",
            mark_price,
            oracle_price,
            sz_decimals,
            max_oracle_bps,
            exit_aggr_bps,
            is_perp=True,
        )
        limit_px = price_info.formatted_exit_px
        _log.info(
            "HL exit pricing side=sell mark_price=%s oracle_price=%s raw_exit_px=%s "
            "oracle_safe_exit_px=%s formatted_exit_px=%s max_deviation_bps=%s "
            "aggressiveness_bps=%s",
            price_info.mark_price,
            price_info.oracle_price,
            price_info.raw_exit_px,
            price_info.oracle_safe_exit_px,
            price_info.formatted_exit_px,
            price_info.max_deviation_bps,
            price_info.aggressiveness_bps,
        )
    elif is_buy and decision.action == ActionType.BUY_STARTER and execution_cfg.get("probe_starter_pricing_mode") == "aggressive":
        oracle_price = getattr(state, "oracle_price", None)
        entry_aggr_bps = float(
            execution_cfg.get(
                "probe_starter_aggressiveness_bps",
                execution_cfg.get("limit_offset_bps", 10),
            )
        )
        max_oracle_bps = float(
            execution_cfg.get(
                "probe_max_oracle_deviation_bps",
                execution_cfg.get(
                    "max_oracle_deviation_bps",
                    (config.hl or {}).get("max_oracle_deviation_bps", 20),
                ),
            )
        )
        price_info = compute_oracle_safe_entry_price(
            "buy",
            mark_price,
            oracle_price,
            sz_decimals,
            max_oracle_bps,
            entry_aggr_bps,
            is_perp=True,
        )
        limit_px = price_info.formatted_exit_px
        _log.info(
            "HL starter probe pricing side=buy mark_price=%s oracle_price=%s raw_probe_px=%s "
            "oracle_safe_probe_px=%s formatted_probe_px=%s max_deviation_bps=%s "
            "aggressiveness_bps=%s tif=%s",
            price_info.mark_price,
            price_info.oracle_price,
            price_info.raw_exit_px,
            price_info.oracle_safe_exit_px,
            price_info.formatted_exit_px,
            price_info.max_deviation_bps,
            price_info.aggressiveness_bps,
            order_tif,
        )
    else:
        limit_px = mark_price * (1 + offset) if is_buy else mark_price * (1 - offset)

    cloid = generate_cloid()

    order_request = HLOrderRequest(
        coin=coin,
        is_buy=is_buy,
        sz=sz,
        limit_px=limit_px,
        order_type={"limit": {"tif": order_tif}},
        sz_decimals=sz_decimals,
        min_size=min_size,
        reduce_only=is_sell,
        cloid=cloid,
    )

    response = place_order(order_request, wallet_address, private_key, client)
    submitted_sz = float(response.submitted_sz) if response.submitted_sz > 0 else sz
    submitted_limit_px = response.submitted_limit_px if response.submitted_limit_px is not None else limit_px

    if response.status == "err":
        error = response.error or "unknown_hl_error"
        error_status = (
            OrderResultStatus.EXECUTION_ERROR
            if error.startswith("order_placement_failed:") or error == "network_error"
            else OrderResultStatus.REJECTED
        )
        return OrderResult(
            status=error_status,
            oid=response.hl_oid,
            client_order_id=response.cloid,
            action=decision.action,
            side="buy" if is_buy else "sell",
            reduce_only=is_sell,
            submitted_qty=submitted_sz,
            filled_qty=0.0,
            remaining_qty=submitted_sz,
            limit_px=submitted_limit_px,
            raw_status_sanitized="error",
            exchange_error_sanitized=error,
            state_before=state.state.value,
            intended_state_after=decision.new_state.value if decision.new_state is not None else None,
            applied_state_after=state.state.value,
            created_at=datetime.now(),
        )

    filled_sz = float(response.filled_sz) if response.filled_sz > 0 else 0.0
    if filled_sz <= 0.0:
        return OrderResult(
            status=OrderResultStatus.SUBMITTED_UNFILLED,
            action=decision.action,
            oid=response.hl_oid,
            client_order_id=response.cloid,
            side="buy" if is_buy else "sell",
            reduce_only=is_sell,
            submitted_qty=submitted_sz,
            filled_qty=0.0,
            remaining_qty=submitted_sz,
            avg_fill_px=None,
            limit_px=submitted_limit_px,
            realized_pnl=0.0,
            commission=0.0,
            raw_status_sanitized="resting",
            state_before=state.state.value,
            intended_state_after=decision.new_state.value if decision.new_state is not None else None,
            applied_state_after=state.state.value,
            created_at=datetime.now(),
        )

    fill_price   = response.avg_fill_px if response.avg_fill_px else submitted_limit_px
    slippage_bps = abs(fill_price - mark_price) / mark_price * 10_000 if mark_price > 0 else 0.0
    commission   = filled_sz * fill_price * HL_TAKER_FEE

    # Audit H4: compute realized PnL via common LIFO helper so HL sells
    # correctly credit thesis_pnl and the max_thesis_loss check works.
    if is_sell:
        realized_pnl = compute_realized_pnl_lifo(state, filled_sz, fill_price)
    else:
        realized_pnl = 0.0

    status = OrderResultStatus.FILLED
    if filled_sz + 1e-12 < submitted_sz:
        status = OrderResultStatus.PARTIALLY_FILLED

    return OrderResult(
        status=status,
        action=decision.action,
        oid=response.hl_oid,
        client_order_id=response.cloid,
        side="buy" if is_buy else "sell",
        reduce_only=is_sell,
        submitted_qty=submitted_sz,
        filled_qty=filled_sz,
        remaining_qty=max(submitted_sz - filled_sz, 0.0),
        avg_fill_px=fill_price,
        limit_px=submitted_limit_px,
        realized_pnl=realized_pnl,
        commission=commission,
        raw_status_sanitized="filled",
        state_before=state.state.value,
        intended_state_after=decision.new_state.value if decision.new_state is not None else None,
        applied_state_after=decision.new_state.value if decision.new_state is not None else state.state.value,
        created_at=datetime.now(),
    )
