"""Strategy sanity audit for supervised soak configs.

This module is observational only. It reports suspicious parameter values and
current-signal context without changing strategy, risk, sizing, or execution.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from src.core.models import BotConfig


AUDIT_SECTIONS = ("entry", "add", "reduce", "take_profit", "risk", "event_risk", "capital")


@dataclass(frozen=True)
class AuditItem:
    parameter: str
    current_value: float
    interpreted_value: str
    interpreted_meaning: str
    assessment: str
    recommendation_aggressive: str
    recommendation_normal: str
    recommendation_conservative: str
    note: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _flatten_numeric(prefix: str, value: Any) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    if isinstance(value, bool):
        return rows
    if isinstance(value, (int, float)):
        rows.append((prefix, float(value)))
        return rows
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_flatten_numeric(child, nested))
        return rows
    if isinstance(value, list):
        for index, nested in enumerate(value):
            rows.extend(_flatten_numeric(f"{prefix}[{index}]", nested))
    return rows


def flatten_audit_parameters(config: BotConfig) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for section in AUDIT_SECTIONS:
        value = getattr(config, section, None)
        if value is None:
            continue
        rows.extend(_flatten_numeric(section, value))
    return rows


def _leaf(parameter: str) -> str:
    return parameter.split(".")[-1].split("[")[-1].rstrip("]")


def interpret_parameter(parameter: str, value: float) -> tuple[str, str]:
    name = _leaf(parameter)
    if name.endswith("_pct") or "_pct_" in name or name in {
        "exposure_pct",
        "additional_exposure_pct",
        "reduce_pct_of_total_position",
        "reduce_pct_of_remaining_position",
        "runner_position_pct_of_original_base",
        "trailing_pct",
        "stop_pct",
    }:
        return f"{value * 100:.1f}%", "fractional percent threshold"
    if name.endswith("_bps") or name == "max_spread_bps":
        return f"{value:.1f} bps", "basis-point threshold"
    if "volume_vs" in name or name.endswith("_volume_vs_20d_avg"):
        return f"{value:.2f}x", "volume ratio threshold"
    if "days" in name:
        return f"{value:.0f} days", "calendar/trading day threshold"
    if "count" in name:
        return f"{value:.0f}", "count threshold"
    if "equity" in name:
        return f"${value:,.0f}", "equity amount"
    return f"{value:g}", "numeric threshold"


def recommendations_for(parameter: str) -> tuple[str, str, str]:
    name = _leaf(parameter)
    if "max_distance_above_ma20_pct" in name or "forbid_add_if_distance_above_ma20_pct" in name:
        return "15%", "8%", "5%"
    if "max_distance_above_ma10_pct" in name or "forbid_add_if_distance_above_ma10_pct" in name:
        return "12%", "8%", "5%"
    if "intraday_gain" in name:
        return "8%", "5%", "3%"
    if "add_trigger_pct" in name or "min_unrealized_profit_before_first_add_pct" in name:
        return "8%", "5%", "3%"
    if "gap_up" in name:
        return "6%", "4%", "2%"
    if "min_volume_vs_20d_avg" in name:
        return "0.8x", "1.0x", "1.2x"
    if "max_down_volume_vs_20d_avg" in name:
        return "2.5x", "2.0x", "1.5x"
    if "drawdown_from_peak_pct" in name:
        return "20%", "12%", "8%"
    if "stop_pct" in name or "trailing_pct" in name:
        return "15%", "10%", "7%"
    if "max_total_capital_at_risk_pct" in name:
        return "5%", "3%", "1.5%"
    if "max_thesis_loss_pct" in name:
        return "6%", "4%", "2%"
    if "max_daily_loss_pct" in name:
        return "3%", "2%", "1%"
    if "max_symbol_exposure_pct" in name:
        return "80%", "50%", "30%"
    if "max_initial_exposure_pct" in name or "max_starter_exposure_pct" in name:
        return "60%", "40%", "25%"
    if "max_addon_exposure_pct" in name or "add_sizes_pct" in parameter:
        return "20%", "10%", "5%"
    if "exposure_cap_pct" in name or "max_exposure" in name:
        return "80%", "50%", "30%"
    if "max_spread_bps" in name:
        return "100 bps", "50 bps", "25 bps"
    return "-", "-", "-"


def classify_parameter(parameter: str, value: float) -> tuple[str, str]:
    name = _leaf(parameter)
    note = ""

    if "enabled" in name:
        return "OK", note

    if name in {"exposure_pct", "additional_exposure_pct"} or name.endswith("_exposure_pct"):
        if value >= 0.80:
            return "UNUSUALLY_PERMISSIVE", "allows near-full account exposure"
        if value >= 0.60:
            return "WARNING", "high exposure for supervised soak"
        if value <= 0.0:
            return "UNUSUALLY_STRICT", "zero exposure prevents this path"
        return "OK", note

    if "max_distance_above" in name or "forbid_add_if_distance_above" in name:
        if value >= 0.50:
            return "UNUSUALLY_PERMISSIVE", "permits entries far above moving average"
        if value >= 0.20:
            return "WARNING", "loose distance filter"
        if 0 < value <= 0.02:
            return "UNUSUALLY_STRICT", "may reject most trend-following entries"
        return "OK", note

    if "intraday_gain" in name or "gap_up" in name:
        if value >= 0.50:
            return "UNUSUALLY_PERMISSIVE", "permits very extended one-bar moves"
        if value >= 0.12:
            return "WARNING", "loose momentum extension filter"
        if 0 < value <= 0.01:
            return "UNUSUALLY_STRICT", "may block normal volatility"
        return "OK", note

    if "min_volume_vs_20d_avg" in name:
        if value <= 0.0:
            return "UNUSUALLY_PERMISSIVE", "volume filter disabled by zero threshold"
        if value < 0.50:
            return "WARNING", "weak volume confirmation"
        if value >= 2.5:
            return "UNUSUALLY_STRICT", "requires unusually high volume"
        return "OK", note

    if "max_down_volume_vs_20d_avg" in name:
        if value >= 10:
            return "UNUSUALLY_PERMISSIVE", "down-volume filter effectively disabled"
        return "OK", note

    if "drawdown_from_peak_pct" in name:
        if value >= 0.50:
            return "UNUSUALLY_PERMISSIVE", "drawdown trigger effectively delayed"
        if value >= 0.25:
            return "WARNING", "loose drawdown trigger"
        return "OK", note

    if "max_total_capital_at_risk_pct" in name:
        if value > 0.05:
            return "WARNING", "above suggested supervised soak risk"
        return "OK", note

    if "max_thesis_loss_pct" in name:
        if value >= 0.10:
            return "UNUSUALLY_PERMISSIVE", "large thesis loss before shutdown"
        if value > 0.05:
            return "WARNING", "above 5% thesis-loss guard"
        return "OK", note

    if "max_daily_loss_pct" in name or "max_intraday_drawdown_pct" in name:
        if value >= 0.05:
            return "WARNING", "large daily/intraday loss threshold"
        return "OK", note

    if "max_intraday_loss_before_halt_pct" in name or "max_gap_down_before_halt_pct" in name:
        if value >= 0.50:
            return "UNUSUALLY_PERMISSIVE", "halt filter effectively disabled"
        if value >= 0.15:
            return "WARNING", "loose halt threshold"
        return "OK", note

    if "max_spread_bps" in name:
        if value > 250:
            return "UNUSUALLY_PERMISSIVE", "allows very wide spreads"
        if value > 100:
            return "WARNING", "wide spread threshold"
        return "OK", note

    if "min_days" in name and value >= 365:
        return "UNUSUALLY_STRICT", "cooldown effectively disables this path"

    if "max_add_count" in name and value == 0:
        return "UNUSUALLY_STRICT", "no pyramid adds allowed"

    if value == 1.0 and any(token in name for token in ("pct", "ratio", "giveback")):
        return "UNUSUALLY_PERMISSIVE", "100% fractional threshold is rarely production-like"

    return "OK", note


def build_strategy_sanity_audit(config: BotConfig, latest_signal: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    items: list[AuditItem] = []
    for parameter, value in flatten_audit_parameters(config):
        interpreted, meaning = interpret_parameter(parameter, value)
        assessment, note = classify_parameter(parameter, value)
        aggressive, normal, conservative = recommendations_for(parameter)
        items.append(AuditItem(
            parameter=parameter,
            current_value=value,
            interpreted_value=interpreted,
            interpreted_meaning=meaning,
            assessment=assessment,
            recommendation_aggressive=aggressive,
            recommendation_normal=normal,
            recommendation_conservative=conservative,
            note=note,
        ))

    warnings = [
        asdict(item) for item in items
        if item.assessment in {"WARNING", "UNUSUALLY_PERMISSIVE", "UNUSUALLY_STRICT"}
    ]
    severity_order = {"UNUSUALLY_PERMISSIVE": 0, "UNUSUALLY_STRICT": 1, "WARNING": 2, "OK": 3}
    warnings.sort(key=lambda row: (severity_order.get(row["assessment"], 9), row["parameter"]))

    return {
        "timestamp": _now_iso(),
        "coin": (config.hl or {}).get("coin", config.symbol.get("ticker", "-")),
        "network": (config.hl or {}).get("network", config.bot.get("mode", "-")),
        "parameters_reviewed": len(items),
        "warning_count": len(warnings),
        "items": [asdict(item) for item in items],
        "warnings": warnings,
        "current_signal": dict(latest_signal or {}),
    }


def audit_output_path(config: BotConfig) -> Path:
    return Path(config.logging.get("run_dir", "reports/hype_hl_testnet_soak")) / "strategy_sanity_audit.json"


def latest_signal_path(config: BotConfig) -> Path:
    return Path(config.logging.get("run_dir", "reports/hype_hl_testnet_soak")) / "latest_signal_diagnostics.json"


def load_latest_signal(config: BotConfig) -> Optional[dict[str, Any]]:
    path = latest_signal_path(config)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


def write_strategy_sanity_audit(config: BotConfig, audit: Mapping[str, Any]) -> Path:
    path = audit_output_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(audit, indent=2, sort_keys=True))
    return path


def strategy_audit_event(config: BotConfig, audit: Mapping[str, Any], report_path: Path) -> dict[str, Any]:
    warnings = list(audit.get("warnings") or [])
    permissive = [row for row in warnings if row.get("assessment") == "UNUSUALLY_PERMISSIVE"]
    restrictive = [row for row in warnings if row.get("assessment") == "UNUSUALLY_STRICT"]
    latest = audit.get("current_signal") if isinstance(audit.get("current_signal"), Mapping) else {}
    most_permissive = permissive[0] if permissive else {}
    most_restrictive = restrictive[0] if restrictive else {}
    return {
        "type": "strategy_audit",
        "event": "strategy_sanity_audit",
        "timestamp": audit.get("timestamp"),
        "run_id": str(config.bot.get("run_id") or "-"),
        "coin": audit.get("coin"),
        "network": audit.get("network"),
        "parameters_reviewed": audit.get("parameters_reviewed"),
        "warning_count": audit.get("warning_count"),
        "most_permissive": (
            f"{most_permissive.get('parameter')} = {most_permissive.get('interpreted_value')}"
            if most_permissive else "-"
        ),
        "most_restrictive": (
            f"{most_restrictive.get('parameter')} = {most_restrictive.get('interpreted_value')}"
            if most_restrictive else "-"
        ),
        "current_signal": latest.get("action"),
        "readiness_score": latest.get("readiness_score"),
        "report_path": report_path.name,
    }
