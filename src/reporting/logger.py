"""CSV writers for orders, positions, risk, decisions (Section 19)."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from src.core.models import (
    Decision, Fill, IndicatorSnapshot, OrderResult, ReconciliationResult, ThesisState,
)


ORDERS_HEADER = [
    "timestamp", "symbol", "action", "side", "qty", "order_type",
    "limit_price", "fill_price", "slippage_bps", "status", "reason",
    "state_before", "intended_state_after", "state_after",
]

POSITIONS_HEADER = [
    "timestamp", "symbol", "state", "qty", "avg_entry_price",
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
    "indicators_snapshot", "config_snapshot_hash", "intended_state_after",
    "applied_state_after", "reconciliation_status", "reconciliation_reason",
    "exchange_position_qty", "local_position_qty", "open_orders_count",
    "matched_fill_count",
]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append(path: Path, header: List[str], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    existing_rows: list[dict] = []
    rewrite_existing = False
    if path.exists():
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            if (reader.fieldnames or []) != header:
                existing_rows = list(reader)
                write_header = True
                rewrite_existing = True
    mode = "w" if rewrite_existing else "a"
    with open(path, mode, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if write_header:
            writer.writeheader()
            for existing in existing_rows:
                writer.writerow({k: existing.get(k, "") for k in header})
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
    *,
    order_result: Optional[OrderResult] = None,
    status_override: Optional[str] = None,
    reason_override: Optional[str] = None,
    qty_override: Optional[float] = None,
) -> None:
    """
    Always writes a row.  When fill is None (no_action / halt), writes a sentinel
    row with status="no_order" so the CSV is updated every cycle.
    """
    reason = reason_override or decision.reason
    qty = decision.qty if qty_override is None else qty_override
    intended_state_after = (
        order_result.intended_state_after
        if order_result is not None
        else (decision.new_state.value if decision.new_state is not None else state_after)
    )

    if status_override == "failed":
        row = {
            "timestamp": _now_iso(),
            "symbol": state.symbol,
            "action": decision.action.value,
            "side": _side(decision),
            "qty": qty,
            "order_type": "limit" if _side(decision) else "",
            "limit_price": "",
            "fill_price": "",
            "slippage_bps": "",
            "status": "failed",
            "reason": reason,
            "state_before": state_before,
            "intended_state_after": intended_state_after,
            "state_after": state_after,
        }
    elif order_result is not None:
        status = order_result.status.value
        fill_price = ""
        slippage_bps = ""
        if fill is not None and fill.qty > 0:
            fill_price = f"{fill.fill_price:.4f}"
            slippage_bps = f"{fill.slippage_bps:.2f}"
        row = {
            "timestamp": order_result.created_at.isoformat() if hasattr(order_result.created_at, "isoformat") else str(order_result.created_at),
            "symbol": state.symbol,
            "action": decision.action.value,
            "side": order_result.side,
            "qty": order_result.submitted_qty if qty_override is None else qty_override,
            "order_type": "limit" if order_result.side else "",
            "limit_price": f"{order_result.limit_px:.4f}" if order_result.limit_px is not None else "",
            "fill_price": fill_price,
            "slippage_bps": slippage_bps,
            "status": status,
            "reason": reason_override or order_result.exchange_error_sanitized or order_result.raw_status_sanitized or reason,
            "state_before": state_before,
            "intended_state_after": intended_state_after,
            "state_after": order_result.applied_state_after or state_after,
        }
    elif fill is not None and fill.qty > 0:
        row = {
            "timestamp": fill.timestamp.isoformat() if hasattr(fill.timestamp, "isoformat") else str(fill.timestamp),
            "symbol": state.symbol,
            "action": decision.action.value,
            "side": _side(decision),
            "qty": fill.qty,
            "order_type": "market",
            "limit_price": "",
            "fill_price": f"{fill.fill_price:.4f}",
            "slippage_bps": f"{fill.slippage_bps:.2f}",
            "status": "filled",
            "reason": reason,
            "state_before": state_before,
            "intended_state_after": intended_state_after,
            "state_after": state_after,
        }
    elif fill is not None:
        row = {
            "timestamp": fill.timestamp.isoformat() if hasattr(fill.timestamp, "isoformat") else str(fill.timestamp),
            "symbol": state.symbol,
            "action": decision.action.value,
            "side": _side(decision),
            "qty": decision.qty,
            "order_type": "limit",
            "limit_price": f"{fill.fill_price:.4f}",
            "fill_price": "",
            "slippage_bps": "",
            "status": "submitted_unfilled",
            "reason": reason,
            "state_before": state_before,
            "intended_state_after": intended_state_after,
            "state_after": state_after,
        }
    else:
        row = {
            "timestamp": _now_iso(),
            "symbol": state.symbol,
            "action": decision.action.value,
            "side": "",
            "qty": 0,
            "order_type": "",
            "limit_price": "",
            "fill_price": "",
            "slippage_bps": "",
            "status": "no_order",
            "reason": reason,
            "state_before": state_before,
            "intended_state_after": intended_state_after,
            "state_after": state_after,
        }
    _append(Path(run_dir) / "orders.csv", ORDERS_HEADER, row)


def log_position(
    run_dir: str, state: ThesisState, indicators: IndicatorSnapshot, equity: float,
) -> None:
    notional = state.current_position_qty * indicators.adj_close
    exposure_pct = notional / equity if equity > 0 else 0.0
    row = {
        "timestamp": _now_iso(),
        "symbol": state.symbol,
        "state": state.state.value,
        "qty": state.current_position_qty,
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
    *,
    risk_status_override: Optional[str] = None,
) -> None:
    notional = state.current_position_qty * indicators.adj_close
    exposure_pct = notional / equity if equity > 0 else 0.0
    peak = state.highest_price_since_entry or indicators.adj_close
    dd_from_peak = (peak - indicators.adj_close) / peak if peak > 0 else 0.0
    avg = state.avg_entry_price or 0.0
    unrealized_pct = (
        (indicators.adj_close - avg) * state.current_position_qty / equity
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
        "daily_pnl": f"{state.daily_pnl:.2f}",
        "risk_status": risk_status_override or ("halted" if state.halted else "ok"),
        "blockers": "|".join(blockers),
    }
    _append(Path(run_dir) / "risk.csv", RISK_HEADER, row)


def log_decision(
    run_dir: str, state: ThesisState, decision: Decision,
    indicators: IndicatorSnapshot, config_hash: str,
    *,
    intended_state_after: Optional[str] = None,
    applied_state_after: Optional[str] = None,
    reconciliation_result: Optional[ReconciliationResult] = None,
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
        "intended_state_after": intended_state_after or (decision.new_state.value if decision.new_state is not None else state.state.value),
        "applied_state_after": applied_state_after or state.state.value,
        "reconciliation_status": reconciliation_result.status.value if reconciliation_result is not None else "",
        "reconciliation_reason": reconciliation_result.reason if reconciliation_result is not None else "",
        "exchange_position_qty": reconciliation_result.exchange_position_qty if reconciliation_result is not None else "",
        "local_position_qty": reconciliation_result.local_position_qty if reconciliation_result is not None else "",
        "open_orders_count": reconciliation_result.open_orders_count if reconciliation_result is not None else "",
        "matched_fill_count": reconciliation_result.matched_fill_count if reconciliation_result is not None else "",
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
    *,
    order_result: Optional[OrderResult] = None,
    reconciliation_result: Optional[ReconciliationResult] = None,
    order_status_override: Optional[str] = None,
    order_reason_override: Optional[str] = None,
    order_qty_override: Optional[float] = None,
    risk_status_override: Optional[str] = None,
) -> None:
    """Write all 4 CSVs (orders, positions, risk, decisions) for one cycle."""
    log_order(
        run_dir,
        state,
        decision,
        fill,
        state_before,
        state_after,
        order_result=order_result,
        status_override=order_status_override,
        reason_override=order_reason_override,
        qty_override=order_qty_override,
    )
    log_position(run_dir, state, indicators, equity)
    log_risk(
        run_dir,
        state,
        indicators,
        equity,
        decision.blockers,
        risk_status_override=risk_status_override,
    )
    log_decision(
        run_dir,
        state,
        decision,
        indicators,
        config_hash,
        intended_state_after=(
            order_result.intended_state_after
            if order_result is not None
            else (decision.new_state.value if decision.new_state is not None else state_after)
        ),
        applied_state_after=order_result.applied_state_after if order_result is not None else state_after,
        reconciliation_result=reconciliation_result,
    )
