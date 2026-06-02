"""Signal diagnostics and calibration logging.

These helpers are observational only. They do not feed back into strategy,
risk, sizing, or execution decisions.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from src.core.models import ActionType, BotConfig, BotState, Decision, IndicatorSnapshot, ThesisState


SIGNAL_DIAGNOSTICS_HEADER = [
    "timestamp",
    "run_id",
    "coin",
    "network",
    "state",
    "action",
    "reason",
    "blockers",
    "blocker_count",
    "no_action_diagnostic",
    "closest_trigger",
    "closest_trigger_distance",
    "readiness_score",
    "close",
    "high",
    "low",
    "volume",
    "spread_bps",
    "funding_rate",
    "ma5",
    "ma10",
    "ma20",
    "ma50",
    "atr",
    "risk_budget_remaining",
    "open_risk",
    "max_risk",
    "collateral_available",
    "condition_matrix_json",
]


@dataclass(frozen=True)
class ConditionEvaluation:
    condition: str
    current_value: Optional[float | str | bool]
    threshold: Optional[float | str | bool]
    status: str
    distance_pct: Optional[float] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _status(condition: bool) -> str:
    return "PASS" if condition else "FAIL"


def _distance_to_min(current: Optional[float], threshold: Optional[float]) -> Optional[float]:
    if current is None or threshold is None or current <= 0:
        return None
    if current >= threshold:
        return 0.0
    return ((threshold / current) - 1.0) * 100.0


def _distance_to_max(current: Optional[float], threshold: Optional[float]) -> Optional[float]:
    if current is None or threshold is None:
        return None
    if current <= threshold:
        return 0.0
    return abs(current - threshold) * 100.0


def _condition(
    condition: str,
    current: Optional[float | str | bool],
    threshold: Optional[float | str | bool],
    passed: bool,
    *,
    distance_pct: Optional[float] = None,
) -> ConditionEvaluation:
    return ConditionEvaluation(
        condition=condition,
        current_value=current,
        threshold=threshold,
        status=_status(passed),
        distance_pct=distance_pct,
    )


def evaluate_entry_conditions(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
    *,
    collateral_available: Optional[bool] = None,
) -> list[ConditionEvaluation]:
    conditions: list[ConditionEvaluation] = []

    thesis_enabled = bool(config.thesis.get("enabled", True)) and bool(state.thesis_enabled)
    conditions.append(_condition("thesis_enabled", thesis_enabled, True, thesis_enabled))

    if collateral_available is not None:
        conditions.append(_condition("collateral_available", collateral_available, True, bool(collateral_available)))

    if state.state != BotState.FLAT:
        conditions.append(ConditionEvaluation("starter_state_flat", state.state.value, BotState.FLAT.value, "SKIPPED"))
        return conditions

    starter = config.entry.get("starter", {})
    if not starter.get("enabled", False):
        conditions.append(ConditionEvaluation("starter_enabled", False, True, "SKIPPED"))
        return conditions

    if starter.get("require_close_above_ma20", True):
        close = indicators.adj_close
        threshold = indicators.ma20
        conditions.append(_condition(
            "close_above_ma20",
            close,
            threshold,
            close > threshold,
            distance_pct=_distance_to_min(close, threshold),
        ))

    max_ma10 = starter.get("max_distance_above_ma10_pct", 0.10)
    conditions.append(_condition(
        "distance_above_ma10_within_limit",
        indicators.distance_from_ma10,
        max_ma10,
        indicators.distance_from_ma10 <= max_ma10,
        distance_pct=_distance_to_max(indicators.distance_from_ma10, max_ma10),
    ))

    max_ma20 = starter.get("max_distance_above_ma20_pct", 0.15)
    conditions.append(_condition(
        "distance_above_ma20_within_limit",
        indicators.distance_from_ma20,
        max_ma20,
        indicators.distance_from_ma20 <= max_ma20,
        distance_pct=_distance_to_max(indicators.distance_from_ma20, max_ma20),
    ))

    max_intraday = starter.get("max_intraday_gain_pct", 0.08)
    conditions.append(_condition(
        "intraday_gain_within_limit",
        indicators.intraday_return,
        max_intraday,
        indicators.intraday_return <= max_intraday,
        distance_pct=_distance_to_max(indicators.intraday_return, max_intraday),
    ))

    max_gap = starter.get("max_gap_up_pct", 0.06)
    conditions.append(_condition(
        "gap_up_within_limit",
        indicators.gap_up_pct,
        max_gap,
        indicators.gap_up_pct <= max_gap,
        distance_pct=_distance_to_max(indicators.gap_up_pct, max_gap),
    ))

    min_vol = starter.get("min_volume_vs_20d_avg", 0.80)
    avg_volume = indicators.avg_volume_20d
    volume_ratio = indicators.volume / avg_volume if avg_volume > 0 else None
    if volume_ratio is None:
        conditions.append(ConditionEvaluation("volume_vs_20d_avg", None, min_vol, "SKIPPED"))
    else:
        conditions.append(_condition(
            "volume_vs_20d_avg",
            volume_ratio,
            min_vol,
            volume_ratio >= min_vol,
            distance_pct=_distance_to_min(volume_ratio, min_vol),
        ))

    days_to_event = state.days_to_event
    event_threshold = config.event_risk.get("stop_new_entry_trading_days_before", 10)
    event_clear = days_to_event is None or not (0 <= days_to_event <= event_threshold)
    conditions.append(_condition("event_window_clear", days_to_event, f">{event_threshold}", event_clear))

    return conditions


def readiness_score(conditions: Iterable[ConditionEvaluation]) -> int:
    active = [condition for condition in conditions if condition.status in {"PASS", "FAIL"}]
    if not active:
        return 0
    passed = sum(1 for condition in active if condition.status == "PASS")
    return int(round((passed / len(active)) * 100))


def closest_trigger(conditions: Iterable[ConditionEvaluation]) -> tuple[str, str]:
    failed = [
        condition for condition in conditions
        if condition.status == "FAIL" and condition.distance_pct is not None
    ]
    if not failed:
        return "", ""
    closest = min(failed, key=lambda condition: abs(condition.distance_pct or 0.0))
    return closest.condition, f"{closest.distance_pct:.2f}%"


def risk_budget_fields(
    state: ThesisState,
    indicators: IndicatorSnapshot,
    config: BotConfig,
) -> dict[str, float]:
    max_risk = float(config.capital["starting_equity"]) * float(config.capital["max_total_capital_at_risk_pct"])
    trailing_stop = state.trailing_stop_price
    lots = ([state.base_lot] if state.base_lot is not None else []) + list(state.addon_lots)
    if trailing_stop is None:
        open_risk = 0.0
    else:
        open_risk = sum(max(0.0, (lot.entry_price - trailing_stop) * lot.qty) for lot in lots)
    remaining = max(0.0, max_risk - open_risk)
    return {
        "risk_budget_remaining": remaining,
        "open_risk": open_risk,
        "max_risk": max_risk,
    }


def no_action_diagnostic(decision: Decision, failed_conditions: list[str]) -> str:
    if decision.action != ActionType.NO_ACTION:
        return ""
    blockers = failed_conditions or list(decision.blockers or [])
    if len(blockers) == 1:
        return f"Only blocker: {blockers[0]}. All other active conditions pass."
    if blockers:
        return "Blockers: " + " ".join(blockers)
    return "No entry blockers detected."


def build_signal_diagnostics(
    *,
    run_id: str,
    coin: str,
    network: str,
    state: ThesisState,
    decision: Decision,
    indicators: IndicatorSnapshot,
    config: BotConfig,
    exchange_position_qty: Optional[float] = None,
    spread_bps: Optional[float] = None,
    collateral_available: Optional[float] = None,
    trading_collateral_available: Optional[bool] = None,
) -> dict[str, Any]:
    conditions = evaluate_entry_conditions(
        state,
        indicators,
        config,
        collateral_available=trading_collateral_available,
    )
    failed = [condition.condition for condition in conditions if condition.status == "FAIL"]
    blockers = list(dict.fromkeys(list(decision.blockers or []) + failed))
    closest_name, closest_distance = closest_trigger(conditions)
    risk = risk_budget_fields(state, indicators, config)
    condition_rows = [asdict(condition) for condition in conditions]
    score = readiness_score(conditions)
    timestamp = _now_iso()

    return {
        "timestamp": timestamp,
        "run_id": run_id,
        "coin": coin,
        "network": network,
        "state": state.state.value,
        "action": decision.action.value,
        "reason": decision.reason,
        "blockers": blockers,
        "blocker_count": len(blockers) if decision.action == ActionType.NO_ACTION else len(decision.blockers or []),
        "no_action_diagnostic": no_action_diagnostic(decision, failed),
        "closest_trigger": closest_name,
        "closest_trigger_distance": closest_distance,
        "readiness_score": score,
        "market": {
            "close": indicators.adj_close,
            "high": indicators.high,
            "low": indicators.low,
            "volume": indicators.volume,
            "spread_bps": spread_bps,
            "funding_rate": state.last_funding_rate,
        },
        "indicators": {
            "ma5": indicators.ma5,
            "ma10": indicators.ma10,
            "ma20": indicators.ma20,
            "ma50": indicators.ma50,
            "atr": indicators.atr14,
        },
        "risk": {
            **risk,
            "collateral_available": collateral_available,
        },
        "exchange_position_qty": exchange_position_qty,
        "local_position_qty": state.current_position_qty,
        "conditions": condition_rows,
    }


def _csv_row(diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    market = diagnostics.get("market", {})
    indicators = diagnostics.get("indicators", {})
    risk = diagnostics.get("risk", {})
    return {
        "timestamp": diagnostics.get("timestamp"),
        "run_id": diagnostics.get("run_id"),
        "coin": diagnostics.get("coin"),
        "network": diagnostics.get("network"),
        "state": diagnostics.get("state"),
        "action": diagnostics.get("action"),
        "reason": diagnostics.get("reason"),
        "blockers": "|".join(str(blocker) for blocker in diagnostics.get("blockers", [])),
        "blocker_count": diagnostics.get("blocker_count"),
        "no_action_diagnostic": diagnostics.get("no_action_diagnostic"),
        "closest_trigger": diagnostics.get("closest_trigger"),
        "closest_trigger_distance": diagnostics.get("closest_trigger_distance"),
        "readiness_score": diagnostics.get("readiness_score"),
        "close": market.get("close"),
        "high": market.get("high"),
        "low": market.get("low"),
        "volume": market.get("volume"),
        "spread_bps": market.get("spread_bps"),
        "funding_rate": market.get("funding_rate"),
        "ma5": indicators.get("ma5"),
        "ma10": indicators.get("ma10"),
        "ma20": indicators.get("ma20"),
        "ma50": indicators.get("ma50"),
        "atr": indicators.get("atr"),
        "risk_budget_remaining": risk.get("risk_budget_remaining"),
        "open_risk": risk.get("open_risk"),
        "max_risk": risk.get("max_risk"),
        "collateral_available": risk.get("collateral_available"),
        "condition_matrix_json": json.dumps(diagnostics.get("conditions", []), sort_keys=True),
    }


def write_signal_diagnostics(run_dir: str, diagnostics: Mapping[str, Any]) -> None:
    path = Path(run_dir)
    path.mkdir(parents=True, exist_ok=True)

    csv_path = path / "signal_diagnostics.csv"
    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SIGNAL_DIAGNOSTICS_HEADER)
        if write_header:
            writer.writeheader()
        writer.writerow(_csv_row(diagnostics))

    snapshot_path = path / "latest_signal_diagnostics.json"
    snapshot_path.write_text(json.dumps(diagnostics, indent=2, sort_keys=True))


def format_signal_check_event(diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    passing = [
        condition["condition"] for condition in diagnostics.get("conditions", [])
        if condition.get("status") == "PASS"
    ]
    blocking = [
        condition["condition"] for condition in diagnostics.get("conditions", [])
        if condition.get("status") == "FAIL"
    ]
    risk = diagnostics.get("risk", {})
    return {
        "type": "signal_check",
        "event": "signal_check",
        "timestamp": diagnostics.get("timestamp"),
        "run_id": diagnostics.get("run_id"),
        "coin": diagnostics.get("coin"),
        "network": diagnostics.get("network"),
        "state": diagnostics.get("state"),
        "action": diagnostics.get("action"),
        "readiness_score": diagnostics.get("readiness_score"),
        "closest_trigger": diagnostics.get("closest_trigger"),
        "closest_trigger_distance": diagnostics.get("closest_trigger_distance"),
        "passing": passing,
        "blocking": blocking,
        "risk_budget_remaining": risk.get("risk_budget_remaining"),
        "max_risk": risk.get("max_risk"),
    }
