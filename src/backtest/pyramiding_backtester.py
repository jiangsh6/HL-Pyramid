from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from src.backtest.backtest_metrics import compute_backtest_metrics
from src.backtest.backtester import bar_interval_to_hours
from src.backtest.reporting import write_backtest_outputs
from src.core.config_loader import config_snapshot_hash
from src.core.decision_engine import run as engine_run
from src.core.models import ActionType, BotConfig, Decision, Fill, IndicatorSnapshot, ThesisState
from src.data.indicators import calc_indicators
from src.execution.utils import compute_realized_pnl_lifo
from src.reporting.signal_diagnostics import build_signal_diagnostics
from src.reporting.state_writer import apply_fill, mark_to_market_state


BUY_ACTIONS = {ActionType.BUY_STARTER, ActionType.BUY_BASE, ActionType.BUY_ADDON}
SELL_ACTIONS = {
    ActionType.SELL_REDUCE_ADDON,
    ActionType.SELL_REDUCE_BASE,
    ActionType.SELL_TAKE_PROFIT,
    ActionType.SELL_STOP,
    ActionType.SELL_TRAILING_STOP,
    ActionType.SELL_EVENT_DERISKING,
    ActionType.EXIT_ALL,
}


@dataclass
class ResearchBacktestResult:
    run_dir: Path
    summary: dict[str, Any]
    orders: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    positions: list[dict[str, Any]]
    final_state: ThesisState


def _has_nan(indicators: IndicatorSnapshot) -> bool:
    values = [
        indicators.adj_close,
        indicators.ma5,
        indicators.ma10,
        indicators.ma20,
        indicators.ma50,
        indicators.atr14,
        indicators.avg_volume_20d,
        indicators.prior_highest_high_20d,
        indicators.drawdown_from_20d_high,
        indicators.distance_from_ma10,
        indicators.distance_from_ma20,
        indicators.intraday_return,
        indicators.gap_up_pct,
        indicators.gap_down_pct,
    ]
    return any(isinstance(value, float) and math.isnan(value) for value in values)


def deterministic_fill(
    *,
    decision: Decision,
    next_open: float,
    state: ThesisState,
    slippage_bps: float,
    timestamp: datetime,
) -> Optional[Fill]:
    if decision.action not in BUY_ACTIONS | SELL_ACTIONS or decision.qty <= 0:
        return None
    slip = slippage_bps / 10000.0
    if decision.action in BUY_ACTIONS:
        px = next_open * (1.0 + slip)
        realized = 0.0
    else:
        px = next_open * (1.0 - slip)
        realized = compute_realized_pnl_lifo(state, decision.qty, px)
    return Fill(
        action=decision.action,
        qty=decision.qty,
        fill_price=px,
        slippage_bps=slippage_bps,
        realized_pnl=realized,
        commission=0.0,
        timestamp=timestamp,
    )


def _risk_budget(state: ThesisState, config: BotConfig) -> tuple[float, float, float]:
    max_risk = config.capital["starting_equity"] * config.capital["max_total_capital_at_risk_pct"]
    if state.trailing_stop_price is None:
        return max_risk, 0.0, 0.0
    lots = ([state.base_lot] if state.base_lot is not None else []) + list(state.addon_lots)
    open_risk = sum(max(0.0, (lot.entry_price - state.trailing_stop_price) * lot.qty) for lot in lots)
    utilization = open_risk / max_risk if max_risk > 0 else 0.0
    return max_risk, open_risk, utilization


def _row_time(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def run_research_backtest(
    *,
    df: pd.DataFrame,
    config: BotConfig,
    coin: str,
    interval: str,
    run_dir: Path,
    run_id: str,
    slippage_bps: float = 1.0,
) -> ResearchBacktestResult:
    if len(df) < 55:
        raise ValueError("insufficient_history_for_backtest")

    state = ThesisState(symbol=coin)
    equity = float(config.capital["starting_equity"])
    cfg_hash = config_snapshot_hash(config)
    interval_hours = bar_interval_to_hours(interval)

    orders: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    signal_rows: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []

    for i in range(len(df) - 1):
        history = df.iloc[: i + 1]
        current_bar = df.iloc[i]
        next_bar = df.iloc[i + 1]
        current_ts = df.index[i]
        next_ts = df.index[i + 1]
        indicators = calc_indicators(history, config)

        if _has_nan(indicators):
            decision = Decision(
                action=ActionType.NO_ACTION,
                reason="insufficient_indicator_history",
                blockers=["nan_indicators"],
                indicators=indicators,
            )
        else:
            decision = engine_run(state, indicators, config, hl_snapshot=None)

        diagnostics = build_signal_diagnostics(
            run_id=run_id,
            coin=coin,
            network=str((config.hl or {}).get("network", config.bot.get("mode", "testnet"))),
            state=state,
            decision=decision,
            indicators=indicators,
            config=config,
            exchange_position_qty=None,
            collateral_available=equity,
            trading_collateral_available=True,
        )
        signal_rows.append({
            "timestamp": diagnostics["timestamp"],
            "run_id": run_id,
            "coin": coin,
            "state": state.state.value,
            "action": decision.action.value,
            "reason": decision.reason,
            "blockers": "|".join(diagnostics.get("blockers", [])),
            "blocker_count": diagnostics["blocker_count"],
            "closest_trigger": diagnostics["closest_trigger"],
            "closest_trigger_distance": diagnostics["closest_trigger_distance"],
            "readiness_score": diagnostics["readiness_score"],
        })

        state_before = state.state.value
        fill = deterministic_fill(
            decision=decision,
            next_open=float(next_bar["open"]),
            state=state,
            slippage_bps=slippage_bps,
            timestamp=next_ts.to_pydatetime() if hasattr(next_ts, "to_pydatetime") else datetime.now(timezone.utc),
        )
        if fill is not None:
            apply_fill(state, fill)
            if decision.new_state is not None:
                state.state = decision.new_state
            equity += fill.realized_pnl
            orders.append({
                "timestamp": _row_time(next_ts),
                "action": decision.action.value,
                "side": "buy" if decision.action in BUY_ACTIONS else "sell",
                "qty": fill.qty,
                "fill_price": fill.fill_price,
                "realized_pnl": fill.realized_pnl,
                "state_before": state_before,
                "state_after": state.state.value,
            })

        mark_to_market_state(state, float(current_bar["close"]))
        mark_equity = equity + state.unrealized_pnl
        max_risk, open_risk, risk_util = _risk_budget(state, config)
        notional = state.current_position_qty * float(current_bar["close"])
        exposure_pct = notional / mark_equity if mark_equity > 0 else 0.0

        decisions.append({
            "timestamp": _row_time(current_ts),
            "action": decision.action.value,
            "reason": decision.reason,
            "blockers": list(decision.blockers or []),
            "readiness_score": diagnostics["readiness_score"],
            "state": state.state.value,
            "config_hash": cfg_hash[:16],
        })
        positions.append({
            "timestamp": _row_time(current_ts),
            "state": state.state.value,
            "qty": state.current_position_qty,
            "avg_entry_price": state.avg_entry_price,
            "unrealized_pnl": state.unrealized_pnl,
            "realized_pnl": state.realized_pnl,
            "add_count": state.add_count,
        })
        equity_rows.append({
            "timestamp": _row_time(current_ts),
            "close": float(current_bar["close"]),
            "equity": mark_equity,
            "cash_equity": equity,
            "position_qty": state.current_position_qty,
            "exposure_pct": exposure_pct,
            "add_count": state.add_count,
            "open_risk": open_risk,
            "max_risk": max_risk,
            "risk_budget_utilization": risk_util,
        })

    equity_curve = pd.DataFrame(equity_rows)
    summary = compute_backtest_metrics(
        equity_curve=equity_curve,
        orders=orders,
        decisions=decisions,
        starting_equity=float(config.capital["starting_equity"]),
        first_close=float(df["close"].iloc[0]),
        last_close=float(df["close"].iloc[-1]),
        interval_hours=interval_hours,
    )
    summary.update({
        "run_id": run_id,
        "coin": coin,
        "interval": interval,
        "bars": len(df),
        "slippage_bps": slippage_bps,
        "output_dir": str(run_dir),
    })

    write_backtest_outputs(
        run_dir=run_dir,
        orders=orders,
        positions=positions,
        decisions=[{**row, "blockers": "|".join(row["blockers"])} for row in decisions],
        signal_diagnostics=signal_rows,
        equity_curve=equity_curve,
        summary=summary,
    )
    return ResearchBacktestResult(
        run_dir=run_dir,
        summary=summary,
        orders=orders,
        decisions=decisions,
        positions=positions,
        final_state=state,
    )
