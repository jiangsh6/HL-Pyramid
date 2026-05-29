"""CSV writers for orders, positions, risk, decisions (Section 19)."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from src.core.models import (
    Decision, Fill, IndicatorSnapshot, ThesisState,
)


ORDERS_HEADER = [
    "timestamp", "symbol", "action", "side", "shares", "order_type",
    "limit_price", "fill_price", "slippage_bps", "status", "reason",
    "state_before", "state_after",
]

POSITIONS_HEADER = [
    "timestamp", "symbol", "state", "shares", "avg_entry_price",
    "last_price", "notional", "exposure_pct", "unrealized_pnl",
    "realized_pnl", "highest_price", "trailing_stop",
]

RISK_HEADER = [
    "timestamp", "symbol", "state", "equity", "exposure_pct", "leverage",
    "drawdown_from_peak", "unrealized_pnl_pct", "peak_unrealized_pnl_pct",
    "thesis_pnl", "daily_pnl", "risk_status", "blockers",
]

DECISIONS_HEADER = [
    "timestamp", "symbol", "state", "decision", "reason", "blockers",
    "indicators_snapshot", "config_snapshot_hash",
]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append(path: Path, header: List[str], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in header})


def _side(decision: Decision) -> str:
    name = decision.action.value
    if name.startswith("buy"):
        return "buy"
    if name.startswith("sell") or name in ("exit_all",):
        return "sell"
    return ""


def log_order(
    run_dir: str, state: ThesisState, decision: Decision,
    fill: Optional[Fill], state_before: str, state_after: str,
) -> None:
    """
    Always writes a row.  When fill is None (no_action / halt), writes a sentinel
    row with status="no_order" so the CSV is updated every cycle.
    """
    if fill is not None:
        row = {
            "timestamp": fill.timestamp.isoformat() if hasattr(fill.timestamp, "isoformat") else str(fill.timestamp),
            "symbol": state.symbol,
            "action": decision.action.value,
            "side": _side(decision),
            "shares": fill.shares,
            "order_type": "market",
            "limit_price": "",
            "fill_price": f"{fill.fill_price:.4f}",
            "slippage_bps": f"{fill.slippage_bps:.2f}",
            "status": "filled",
            "reason": decision.reason,
            "state_before": state_before,
            "state_after": state_after,
        }
    else:
        row = {
            "timestamp": _now_iso(),
            "symbol": state.symbol,
            "action": decision.action.value,
            "side": "",
            "shares": 0,
            "order_type": "",
            "limit_price": "",
            "fill_price": "",
            "slippage_bps": "",
            "status": "no_order",
            "reason": decision.reason,
            "state_before": state_before,
            "state_after": state_after,
        }
    _append(Path(run_dir) / "orders.csv", ORDERS_HEADER, row)


def log_position(
    run_dir: str, state: ThesisState, indicators: IndicatorSnapshot, equity: float,
) -> None:
    notional = state.current_position_shares * indicators.adj_close
    exposure_pct = notional / equity if equity > 0 else 0.0
    row = {
        "timestamp": _now_iso(),
        "symbol": state.symbol,
        "state": state.state.value,
        "shares": state.current_position_shares,
        "avg_entry_price": f"{state.avg_entry_price:.4f}" if state.avg_entry_price else "",
        "last_price": f"{indicators.adj_close:.4f}",
        "notional": f"{notional:.2f}",
        "exposure_pct": f"{exposure_pct:.4f}",
        "unrealized_pnl": f"{state.unrealized_pnl:.2f}",
        "realized_pnl": f"{state.realized_pnl:.2f}",
        "highest_price": f"{state.highest_price_since_entry:.4f}" if state.highest_price_since_entry else "",
        "trailing_stop": f"{state.trailing_stop_price:.4f}" if state.trailing_stop_price else "",
    }
    _append(Path(run_dir) / "positions.csv", POSITIONS_HEADER, row)


def log_risk(
    run_dir: str, state: ThesisState, indicators: IndicatorSnapshot,
    equity: float, blockers: List[str],
) -> None:
    notional = state.current_position_shares * indicators.adj_close
    exposure_pct = notional / equity if equity > 0 else 0.0
    peak = state.highest_price_since_entry or indicators.adj_close
    dd_from_peak = (peak - indicators.adj_close) / peak if peak > 0 else 0.0
    avg = state.avg_entry_price or 0.0
    unrealized_pct = (
        (indicators.adj_close - avg) * state.current_position_shares / equity
        if (avg > 0 and equity > 0) else 0.0
    )
    row = {
        "timestamp": _now_iso(),
        "symbol": state.symbol,
        "state": state.state.value,
        "equity": f"{equity:.2f}",
        "exposure_pct": f"{exposure_pct:.4f}",
        "leverage": "1.00",
        "drawdown_from_peak": f"{dd_from_peak:.4f}",
        "unrealized_pnl_pct": f"{unrealized_pct:.4f}",
        "peak_unrealized_pnl_pct": f"{state.peak_unrealized_pnl_pct:.4f}",
        "thesis_pnl": f"{state.thesis_pnl:.2f}",
        "daily_pnl": f"{state.realized_pnl:.2f}",
        "risk_status": "halted" if state.halted else "ok",
        "blockers": "|".join(blockers),
    }
    _append(Path(run_dir) / "risk.csv", RISK_HEADER, row)


def log_decision(
    run_dir: str, state: ThesisState, decision: Decision,
    indicators: IndicatorSnapshot, config_hash: str,
) -> None:
    snap = (
        f"close={indicators.adj_close:.2f},ma5={indicators.ma5:.2f},"
        f"ma10={indicators.ma10:.2f},ma20={indicators.ma20:.2f},"
        f"ma50={indicators.ma50:.2f},atr14={indicators.atr14:.2f}"
    )
    row = {
        "timestamp": _now_iso(),
        "symbol": state.symbol,
        "state": state.state.value,
        "decision": decision.action.value,
        "reason": decision.reason,
        "blockers": "|".join(decision.blockers),
        "indicators_snapshot": snap,
        "config_snapshot_hash": config_hash[:16],
    }
    _append(Path(run_dir) / "decisions.csv", DECISIONS_HEADER, row)


def log_cycle(
    run_dir: str,
    state: ThesisState,
    decision: Decision,
    fill: Optional[Fill],
    indicators: IndicatorSnapshot,
    equity: float,
    state_before: str,
    state_after: str,
    config_hash: str,
) -> None:
    """Write all 4 CSVs (orders, positions, risk, decisions) for one cycle."""
    log_order(run_dir, state, decision, fill, state_before, state_after)
    log_position(run_dir, state, indicators, equity)
    log_risk(run_dir, state, indicators, equity, decision.blockers)
    log_decision(run_dir, state, decision, indicators, config_hash)
