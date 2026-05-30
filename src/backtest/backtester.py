"""
Backtest engine (Section 21.3).

run_backtest(df, config, *, run_dir=None) -> BacktestResult

The backtest loop mirrors the live EOD decision cycle exactly:
  1. Update trailing stop from bar T HIGH  ← BEFORE decision
  2. calc_indicators(df.iloc[:i+1], ...)   ← no look-ahead
  3. decision_engine.run()
  4. Fill at bar T+1 open via paper_broker
  5. apply_fill()
  6. Log

Gap risk: if bar T+1 open < trailing_stop_price, fill executes at bar T+1 open
(no guaranteed stop price). gap_loss flag is set on the record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from src.core.config_loader import config_snapshot_hash
from src.core.decision_engine import run as engine_run
from src.core.models import (
    ActionType, BotConfig, BotState, ThesisState,
)
from src.data.indicators import calc_indicators
from src.execution.paper_broker import execute as paper_execute
from src.hl.funding import apply_funding_to_state, calc_funding_payment
from src.reporting.logger import log_cycle
from src.reporting.state_writer import apply_fill
from src.strategy.stops import _determine_trailing_pct
from src.backtest.metrics import BacktestRecord, compute_metrics, REQUIRED_METRIC_KEYS


@dataclass
class BacktestResult:
    records: List[BacktestRecord]
    metrics: Dict[str, Any]
    final_state: ThesisState


def _in_event_window(state: ThesisState, config: BotConfig) -> bool:
    days = state.days_to_event
    if days is None:
        return False
    thresh = config.event_risk.get("stop_adding_trading_days_before", 10)
    return 0 <= days <= thresh


def run_backtest(
    df: pd.DataFrame,
    config: BotConfig,
    *,
    run_dir: Optional[str] = None,
    ticker: Optional[str] = None,
    initial_state: Optional[ThesisState] = None,
    funding_rates: Optional[dict] = None,
) -> BacktestResult:
    """
    Run the full backtest over `df`.

    df must have columns: open, high, low, close, adj_close, volume
    with a DatetimeIndex.

    Requires at least config.data['required_history_days'] bars before
    the first decision bar (those warm-up bars are skipped for decisions).

    Parameters
    ----------
    df : pd.DataFrame
        Historical OHLCV data.
    config : BotConfig
        Loaded and validated bot configuration.
    run_dir : str, optional
        If supplied, all 4 CSVs are written there each cycle (same as live mode).
    ticker : str, optional
        Override state symbol; defaults to config.symbol['ticker'].
    """
    required_history = config.data.get("required_history_days", 120)
    symbol = ticker or config.symbol.get("ticker", "UNKNOWN")
    equity = config.capital["starting_equity"]
    cfg_hash = config_snapshot_hash(config)

    state = initial_state if initial_state is not None else ThesisState(symbol=symbol)
    bar_hours = bar_interval_to_hours(config.data.get("bar_interval", "1d"))

    records: List[BacktestRecord] = []

    bars = df.reset_index()  # rows with 'date' column + ohlcv
    n = len(bars)

    for i in range(n - 1):           # need bar T+1 for fill → stop one short
        row_t   = bars.iloc[i]
        row_t1  = bars.iloc[i + 1]

        # ── warm-up: need at least required_history bars of indicators ─────
        # We always run decisions; indicators return NaN for short windows and
        # the engine will halt.  Skip decisions in the warm-up window instead.
        if i < required_history - 1:
            records.append(_make_warmup_record(row_t, state, equity))
            continue

        # ── Step 1: update trailing stop from bar T HIGH (BEFORE decision) ──
        # The decision engine also calls update_trailing_stop internally at
        # Step 4, but that uses indicators.high which is the same value.
        # We replicate the standalone update here so the backtest loop is
        # self-contained and identical to Section 21.3.
        if state.current_position_shares > 0:
            bar_high = float(row_t["high"])
            state.highest_price_since_entry = max(
                state.highest_price_since_entry or 0.0, bar_high
            )
            avg = state.avg_entry_price or float(row_t["adj_close"])
            adj = float(row_t["adj_close"])
            profit_from_avg = (adj - avg) / avg if avg > 0 else 0.0
            trailing_pct = _determine_trailing_pct(profit_from_avg, config)
            candidate = state.highest_price_since_entry * (1 - trailing_pct)
            state.trailing_stop_price = max(
                state.trailing_stop_price or 0.0, candidate
            )

        # ── Step 2: indicators up to bar T only (no look-ahead) ──────────────
        indicators = calc_indicators(df.iloc[: i + 1], config)

        state_before = state.state.value

        # ── Step 3: decision ──────────────────────────────────────────────────
        decision = engine_run(state, indicators, config)

        # ── Step 4: fill at bar T+1 open ──────────────────────────────────────
        fill = None
        gap_loss = False
        fill_price_used: Optional[float] = None
        fill_shares_signed = 0
        fill_realized_pnl = 0.0
        funding_payment = 0.0
        next_open = float(row_t1["open"])

        if decision.action not in (ActionType.NO_ACTION, ActionType.HALT):
            # Gap risk: fill at next_open regardless — even if it gapped
            # through the trailing stop.
            if (state.trailing_stop_price is not None
                    and next_open < state.trailing_stop_price
                    and decision.action in (
                        ActionType.SELL_STOP, ActionType.SELL_TRAILING_STOP,
                        ActionType.EXIT_ALL,
                    )):
                gap_loss = True

            fill = paper_execute(decision, next_open, state, config)
            apply_fill(state, fill)
            fill_price_used = fill.fill_price

            # Determine sign of shares (positive = buy, negative = sell)
            buy_actions = {ActionType.BUY_STARTER, ActionType.BUY_BASE, ActionType.BUY_ADDON}
            fill_shares_signed = (
                fill.shares if decision.action in buy_actions else -fill.shares
            )

            # Capture realized PnL from the Fill object before state is mutated
            # further. paper_broker already computes this via LIFO cost basis.
            if decision.action not in buy_actions:
                fill_realized_pnl = fill.realized_pnl

            # Apply new_state from decision if provided
            if decision.new_state is not None and decision.new_state != state.state:
                state.state = decision.new_state

        if config.data.get("source") == "hyperliquid":
            bar_date = row_t.get("date", row_t.name)
            rate = (funding_rates or {}).get(bar_date, 0.0)
            if state.current_position_contracts > 0 and rate != 0.0:
                funding_payment = calc_funding_payment(
                    rate,
                    state.current_position_contracts,
                    float(row_t["close"]),
                    hours=bar_hours,
                )
                state = apply_funding_to_state(state, funding_payment)

        state_after = state.state.value

        # ── Step 5: log ────────────────────────────────────────────────────────
        exposure_pct = (
            state.current_position_shares * float(row_t["adj_close"]) / equity
            if equity > 0 else 0.0
        )

        rec = BacktestRecord(
            bar_date=row_t.get("date", row_t.name),
            adj_close=float(row_t["adj_close"]),
            open_price=float(row_t["open"]),
            shares=state.current_position_shares,
            avg_entry_price=state.avg_entry_price,
            fill_price=fill_price_used,
            fill_shares=fill_shares_signed,
            action=decision.action,
            state_name=state_after,
            in_event_window=_in_event_window(state, config),
            exposure_pct=exposure_pct,
            trailing_stop_price=state.trailing_stop_price,
            gap_loss=gap_loss,
            fill_realized_pnl=fill_realized_pnl,
            funding_payment=funding_payment,
        )
        records.append(rec)

        if run_dir is not None:
            log_cycle(
                run_dir=run_dir,
                state=state,
                decision=decision,
                fill=fill,
                indicators=indicators,
                equity=equity,
                state_before=state_before,
                state_after=state_after,
                config_hash=cfg_hash,
            )

    # ── metrics ───────────────────────────────────────────────────────────────
    decision_records = [r for r in records if r.action != ActionType.NO_ACTION
                        or r.shares > 0]

    first_close = float(df["adj_close"].iloc[0]) if len(df) > 0 else None
    last_close  = float(df["adj_close"].iloc[-1]) if len(df) > 0 else None

    metrics = compute_metrics(
        records,
        config,
        first_bar_price=first_close,
        last_bar_price=last_close,
    )

    return BacktestResult(records=records, metrics=metrics, final_state=state)


def bar_interval_to_hours(interval: str) -> float:
    """Convert supported bar interval strings to hours."""
    mapping = {
        "1m": 1.0 / 60.0,
        "5m": 5.0 / 60.0,
        "15m": 0.25,
        "1h": 1.0,
        "4h": 4.0,
        "1d": 24.0,
    }
    try:
        return mapping[interval]
    except KeyError as exc:
        raise ValueError(f"Unsupported bar interval: {interval}") from exc


def _make_warmup_record(row: Any, state: ThesisState, equity: float) -> BacktestRecord:
    """Placeholder record for warm-up bars (no decisions made)."""
    adj = float(row["adj_close"])
    return BacktestRecord(
        bar_date=row.get("date", getattr(row, "name", None)),
        adj_close=adj,
        open_price=float(row["open"]),
        shares=0,
        avg_entry_price=None,
        fill_price=None,
        fill_shares=0,
        action=ActionType.NO_ACTION,
        state_name=state.state.value,
        in_event_window=False,
        exposure_pct=0.0,
        trailing_stop_price=None,
        gap_loss=False,
    )
