"""
All backtest metrics (Section 21.4 + HL funding extension).

compute_metrics(records, config) -> dict

`records` is the list of BacktestRecord objects produced by the backtest loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.core.models import ActionType, BotConfig


@dataclass
class BacktestRecord:
    """One bar's worth of backtest data, built by the backtester each iteration."""
    bar_date: Any                   # date
    adj_close: float
    open_price: float               # raw open (used for fill)
    shares: int                     # position shares at END of this bar
    avg_entry_price: Optional[float]
    fill_price: Optional[float]     # fill price if an order executed this bar
    fill_shares: int                # shares transacted (positive = buy, negative = sell)
    action: ActionType
    state_name: str
    in_event_window: bool
    exposure_pct: float             # position notional / equity at bar close
    trailing_stop_price: Optional[float]
    gap_loss: bool                  # fill gapped below trailing stop
    fill_realized_pnl: float = 0.0  # realized PnL from Fill.realized_pnl (sell fills only)
    funding_payment: float = 0.0    # negative = paid by long, positive = received


def compute_metrics(
    records: List[BacktestRecord],
    config: BotConfig,
    first_bar_price: Optional[float] = None,
    last_bar_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Compute all required metrics from the backtest record list.

    Returns a flat dict with exactly the 19 keys listed in Section 21.4.
    """
    equity = config.capital["starting_equity"]

    # ── running totals ────────────────────────────────────────────────────────
    number_of_entries    = 0
    number_of_adds       = 0
    number_of_reduces    = 0
    number_of_take_profits = 0
    number_of_stops      = 0
    event_window_pnl     = 0.0
    gap_loss_count       = 0

    wins: List[float] = []
    losses: List[float] = []
    bars_in_market       = 0
    exposures: List[float] = []

    realized_pnl         = 0.0   # running tally
    funding_pnl          = 0.0
    peak_equity          = equity
    max_drawdown         = 0.0
    max_intraday_drawdown = 0.0
    max_exposure         = 0.0

    for rec in records:
        # Classify action
        if rec.action in (ActionType.BUY_STARTER, ActionType.BUY_BASE):
            number_of_entries += 1
        elif rec.action == ActionType.BUY_ADDON:
            number_of_adds += 1
        elif rec.action in (ActionType.SELL_REDUCE_ADDON, ActionType.SELL_REDUCE_BASE):
            number_of_reduces += 1
        elif rec.action == ActionType.SELL_TAKE_PROFIT:
            number_of_take_profits += 1
        elif rec.action in (ActionType.SELL_STOP, ActionType.SELL_TRAILING_STOP,
                            ActionType.EXIT_ALL):
            number_of_stops += 1

        # Gap losses
        if rec.gap_loss:
            gap_loss_count += 1

        funding_pnl += rec.funding_payment

        # PnL from fills (sells only).
        # Use fill_realized_pnl captured from Fill.realized_pnl at fill time.
        # This avoids the post-apply_fill avg_entry_price=None bug on full exits.
        if rec.fill_shares < 0 and rec.fill_price is not None:
            pnl = rec.fill_realized_pnl
            realized_pnl += pnl
            if rec.in_event_window:
                event_window_pnl += pnl
            if pnl > 0:
                wins.append(pnl)
            elif pnl < 0:
                losses.append(pnl)

        # Track exposure
        exposures.append(rec.exposure_pct)
        max_exposure = max(max_exposure, rec.exposure_pct)

        # In-market tracking
        if rec.shares > 0:
            bars_in_market += 1

        # Equity curve / drawdown
        current_equity = equity + realized_pnl + funding_pnl
        if rec.shares > 0 and rec.avg_entry_price:
            unrealized = (rec.adj_close - rec.avg_entry_price) * rec.shares
            current_equity += unrealized

        peak_equity = max(peak_equity, current_equity)
        dd = (peak_equity - current_equity) / peak_equity if peak_equity > 0 else 0.0
        max_drawdown = max(max_drawdown, dd)

        # Intraday drawdown approximation: from entry price to low (use open as proxy)
        if rec.shares > 0 and rec.avg_entry_price and rec.open_price < rec.adj_close:
            intra_loss = (rec.avg_entry_price - rec.open_price) * rec.shares
            if intra_loss > 0:
                intra_dd = intra_loss / equity
                max_intraday_drawdown = max(max_intraday_drawdown, intra_dd)

    # ── aggregate statistics ──────────────────────────────────────────────────
    total_bars = len(records)

    total_return = (realized_pnl + funding_pnl) / equity if equity > 0 else 0.0

    # Buy-and-hold: based on first / last bar adj_close
    bah_return = 0.0
    if first_bar_price and last_bar_price and first_bar_price > 0:
        bah_return = (last_bar_price - first_bar_price) / first_bar_price

    excess_return = total_return - bah_return

    time_in_market = bars_in_market / total_bars if total_bars > 0 else 0.0
    average_exposure = sum(exposures) / len(exposures) if exposures else 0.0

    win_rate = len(wins) / (len(wins) + len(losses)) if (wins or losses) else 0.0
    average_win  = sum(wins) / len(wins)   if wins   else 0.0
    average_loss = sum(losses) / len(losses) if losses else 0.0
    largest_loss = min(losses) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "total_return":               total_return,
        "buy_and_hold_return":        bah_return,
        "excess_return_vs_buy_hold":  excess_return,
        "max_drawdown":               max_drawdown,
        "max_intraday_drawdown":      max_intraday_drawdown,
        "number_of_entries":          number_of_entries,
        "number_of_adds":             number_of_adds,
        "number_of_reduces":          number_of_reduces,
        "number_of_take_profits":     number_of_take_profits,
        "number_of_stops":            number_of_stops,
        "win_rate":                   win_rate,
        "average_win":                average_win,
        "average_loss":               average_loss,
        "largest_loss":               largest_loss,
        "profit_factor":              profit_factor,
        "time_in_market":             time_in_market,
        "average_exposure":           average_exposure,
        "max_exposure":               max_exposure,
        "event_window_pnl":           event_window_pnl,
        "gap_loss_count":             gap_loss_count,
        "funding_pnl":                funding_pnl,
    }


REQUIRED_METRIC_KEYS = frozenset([
    "total_return", "buy_and_hold_return", "excess_return_vs_buy_hold",
    "max_drawdown", "max_intraday_drawdown",
    "number_of_entries", "number_of_adds", "number_of_reduces",
    "number_of_take_profits", "number_of_stops",
    "win_rate", "average_win", "average_loss", "largest_loss", "profit_factor",
    "time_in_market", "average_exposure", "max_exposure",
    "event_window_pnl", "gap_loss_count", "funding_pnl",
])
