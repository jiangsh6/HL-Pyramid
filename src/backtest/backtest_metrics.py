from __future__ import annotations

from collections import Counter
from math import sqrt
from typing import Any

import pandas as pd


def _max_drawdown(values: pd.Series) -> float:
    if values.empty:
        return 0.0
    peak = values.cummax()
    dd = values / peak - 1.0
    return float(dd.min() * 100.0)


def compute_backtest_metrics(
    *,
    equity_curve: pd.DataFrame,
    orders: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    starting_equity: float,
    first_close: float,
    last_close: float,
    interval_hours: float,
) -> dict[str, Any]:
    equity = equity_curve["equity"] if not equity_curve.empty else pd.Series([starting_equity])
    returns = equity.pct_change().dropna()
    total_return = (float(equity.iloc[-1]) / starting_equity - 1.0) * 100.0 if starting_equity else 0.0
    bh_return = (last_close / first_close - 1.0) * 100.0 if first_close else 0.0
    periods_per_year = 365.0 * 24.0 / interval_hours if interval_hours > 0 else 0.0
    annualized = 0.0
    if len(equity) > 1 and periods_per_year > 0:
        years = len(equity) / periods_per_year
        if years > 0:
            annualized = ((float(equity.iloc[-1]) / starting_equity) ** (1.0 / years) - 1.0) * 100.0
    bh_equity = starting_equity * (equity_curve["close"] / first_close) if not equity_curve.empty and first_close else pd.Series()

    sell_pnls = [float(order.get("realized_pnl", 0.0) or 0.0) for order in orders if str(order.get("side")) == "sell"]
    wins = [pnl for pnl in sell_pnls if pnl > 0]
    losses = [pnl for pnl in sell_pnls if pnl < 0]
    blocker_counter: Counter[str] = Counter()
    readiness_values: list[float] = []
    for decision in decisions:
        for blocker in decision.get("blockers", []):
            blocker_counter[str(blocker)] += 1
        if decision.get("readiness_score") is not None:
            readiness_values.append(float(decision["readiness_score"]))

    in_market = equity_curve["position_qty"] > 0 if not equity_curve.empty else pd.Series(dtype=bool)
    adds = [o for o in orders if o.get("action") == "buy_addon"]
    entries = [o for o in orders if o.get("action") in {"buy_starter", "buy_base"}]
    exits = [o for o in orders if o.get("side") == "sell"]
    stops = [o for o in orders if "stop" in str(o.get("action", ""))]
    exposure = equity_curve["exposure_pct"] if not equity_curve.empty else pd.Series(dtype=float)
    risk_util = equity_curve["risk_budget_utilization"] if not equity_curve.empty else pd.Series(dtype=float)

    return {
        "strategy_scope": "starter_add_stop_only",
        "total_return_pct": total_return,
        "annualized_return_pct": annualized,
        "buy_and_hold_return_pct": bh_return,
        "excess_return_vs_buy_hold_pct": total_return - bh_return,
        "max_drawdown_pct": _max_drawdown(equity),
        "buy_and_hold_max_drawdown_pct": _max_drawdown(bh_equity),
        "realized_volatility": float(returns.std() * sqrt(periods_per_year) * 100.0) if len(returns) > 1 else 0.0,
        "downside_volatility": float(returns[returns < 0].std() * sqrt(periods_per_year) * 100.0) if len(returns[returns < 0]) > 1 else 0.0,
        "worst_trade_pct": min(sell_pnls) if sell_pnls else 0.0,
        "number_of_entries": len(entries),
        "number_of_adds": len(adds),
        "number_of_exits": len(exits),
        "number_of_stops": len(stops),
        "average_hold_hours": 0.0,
        "time_in_market_pct": float(in_market.mean() * 100.0) if len(in_market) else 0.0,
        "win_rate": len(wins) / len(sell_pnls) * 100.0 if sell_pnls else 0.0,
        "avg_win_pct": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_pct": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0),
        "max_add_count_reached": max((int(row.get("add_count", 0)) for row in equity_curve.to_dict("records")), default=0),
        "average_add_count": float(equity_curve["add_count"].mean()) if not equity_curve.empty else 0.0,
        "avg_exposure_pct": float(exposure.mean()) if not exposure.empty else 0.0,
        "max_exposure_pct": float(exposure.max()) if not exposure.empty else 0.0,
        "risk_budget_utilization_avg": float(risk_util.mean()) if not risk_util.empty else 0.0,
        "risk_budget_utilization_max": float(risk_util.max()) if not risk_util.empty else 0.0,
        "no_action_count": sum(1 for d in decisions if d.get("action") == "no_action"),
        "top_blockers": blocker_counter.most_common(10),
        "average_readiness_score": sum(readiness_values) / len(readiness_values) if readiness_values else 0.0,
        "max_readiness_score": max(readiness_values) if readiness_values else 0.0,
        "readiness_gt_80_count": sum(1 for v in readiness_values if v > 80),
        "readiness_gt_90_count": sum(1 for v in readiness_values if v > 90),
    }
