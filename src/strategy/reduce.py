from __future__ import annotations

from typing import List, Tuple

from src.core.models import (
    EPSILON, ActionType, BotConfig, BotState, Decision, IndicatorSnapshot, LotRecord, ThesisState,
)


def lifo_reduce(
    addon_lots: List[LotRecord],
    qty_to_sell: float = 0.0,
    sz_decimals: int = 0,
    min_size: float = 0.0,
) -> Tuple[List[LotRecord], float]:
    """
    Reduce add-on lots LIFO (latest lot first).  Section 13.1.
    """
    remaining = float(qty_to_sell)
    updated: List[LotRecord] = []
    for lot in reversed(addon_lots):
        if remaining <= EPSILON:
            updated.insert(0, lot)
            continue
        sell              = min(lot.qty, remaining)
        remaining        -= sell
        new_qty           = round(lot.qty - sell, sz_decimals) if sz_decimals > 0 else lot.qty - sell
        if new_qty > max(min_size, EPSILON):
            updated.insert(
                0,
                LotRecord(
                    lot_id=lot.lot_id,
                    entry_price=lot.entry_price,
                    qty=new_qty,
                    entry_date=lot.entry_date,
                ),
            )
    return updated, float(qty_to_sell) - remaining


def drawdown_from_peak(state: ThesisState, indicators: IndicatorSnapshot) -> float:
    peak = state.highest_price_since_entry
    if peak is None or peak <= 0:
        return 0.0
    return (peak - indicators.adj_close) / peak


def _new_state_for_reduce(state: ThesisState) -> Optional[BotState]:
    return None if state.state == BotState.REDUCE_MODE else BotState.REDUCE_MODE


def check_addon_reduce(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """Section 13.2 with cascade dedup (8.3)."""
    if not state.addon_lots:
        return None
    if not config.reduce.get("enabled", False):
        return None
    red_cfg = config.reduce.get("addon_reduce", {})

    rule_a = (
        red_cfg.get("reduce_latest_add_if_close_below_ma5", False)
        and indicators.adj_close < indicators.ma5
    )
    rule_b = (
        red_cfg.get("reduce_all_addons_if_close_below_ma10", False)
        and indicators.adj_close < indicators.ma10
    )
    dd_thresh = red_cfg.get("reduce_all_addons_if_drawdown_from_peak_pct", 0.10)
    rule_c = drawdown_from_peak(state, indicators) >= dd_thresh

    if rule_b or rule_c:
        total = sum(l.qty for l in state.addon_lots)
        return Decision(
            action=ActionType.SELL_REDUCE_ADDON,
            qty=total,
            reason="addon_reduce_all_" + ("ma10" if rule_b else "drawdown"),
            new_state=_new_state_for_reduce(state),
            indicators=indicators,
        )
    if rule_a:
        latest = state.addon_lots[-1]
        return Decision(
            action=ActionType.SELL_REDUCE_ADDON,
            qty=latest.qty,
            reason="addon_reduce_latest_ma5",
            new_state=_new_state_for_reduce(state),
            indicators=indicators,
        )
    return None


def check_base_reduce(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> Optional[Decision]:
    """Section 13.3 with cascade dedup (Rule F overrides D and E)."""
    if state.base_lot is None or state.base_lot.qty <= EPSILON:
        return None
    if not config.reduce.get("enabled", False):
        return None
    red_cfg = config.reduce.get("base_reduce", {})

    rule_f = (
        red_cfg.get("reduce_base_all_if_close_below_ma50", False)
        and indicators.adj_close < indicators.ma50
    )
    if rule_f:
        return Decision(
            action=ActionType.EXIT_ALL,
            qty=state.current_position_qty,
            reason="base_reduce_exit_ma50",
            new_state=BotState.EXITED,
            indicators=indicators,
        )

    rule_d = (
        red_cfg.get("reduce_base_half_if_close_below_ma20", False)
        and indicators.adj_close < indicators.ma20
    )
    dd_thresh = red_cfg.get("reduce_base_half_if_drawdown_from_peak_pct", 0.15)
    rule_e = drawdown_from_peak(state, indicators) >= dd_thresh

    if rule_d or rule_e:
        half = state.base_lot.qty * 0.5
        if half <= EPSILON:
            return None
        return Decision(
            action=ActionType.SELL_REDUCE_BASE,
            qty=half,
            reason="base_reduce_half_" + ("ma20" if rule_d else "drawdown"),
            new_state=_new_state_for_reduce(state),
            indicators=indicators,
        )
    return None


def recalc_avg_entry_price(state: ThesisState) -> None:
    """
    Section 7.2.  Mutates avg_entry_price and current_position_qty.
    """
    lots = (
        [state.base_lot] if state.base_lot is not None and state.base_lot.qty > EPSILON
        else []
    )
    lots = lots + [l for l in state.addon_lots if l.qty > EPSILON]
    total_qty = sum(l.qty for l in lots)
    if total_qty <= EPSILON:
        state.avg_entry_price               = None
        state.current_position_qty    = 0.0
        return
    weighted = sum(l.qty * l.entry_price for l in lots)
    state.avg_entry_price               = weighted / total_qty
    state.current_position_qty          = total_qty
