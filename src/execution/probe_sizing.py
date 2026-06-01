from __future__ import annotations

from typing import Optional

from src.core.models import EPSILON, BotConfig


def _probe_cfg(config: BotConfig) -> dict:
    return config.probe or {}


def get_probe_base_qty(config: BotConfig, price: float, explicit_qty: Optional[float] = None) -> float:
    del price
    if explicit_qty is not None:
        return float(explicit_qty)
    probe = _probe_cfg(config)
    if "default_base_qty" in probe:
        return float(probe["default_base_qty"])
    return float((config.hl or {}).get("min_size", 0.001) or 0.001)


def get_probe_addon_qty(config: BotConfig, price: float, explicit_qty: Optional[float] = None) -> float:
    del price
    if explicit_qty is not None:
        return float(explicit_qty)
    probe = _probe_cfg(config)
    if "default_addon_qty" in probe:
        return float(probe["default_addon_qty"])
    return float((config.hl or {}).get("min_size", 0.001) or 0.001)


def get_probe_runner_pct(config: BotConfig, explicit_runner_pct: Optional[float] = None) -> float:
    if explicit_runner_pct is not None:
        return float(explicit_runner_pct)
    probe = _probe_cfg(config)
    return float(probe.get("runner_pct", 0.5))


def get_probe_qty_multiplier(config: BotConfig) -> float:
    probe = _probe_cfg(config)
    return float(probe.get("min_probe_qty_multiplier", 5.0))


def validate_probe_qty_above_min(
    qty: float,
    min_size: float,
    multiplier: float = 5.0,
    *,
    allow_min_size_edge_test: bool = False,
) -> Optional[str]:
    if qty <= EPSILON:
        return "probe_qty_non_positive"
    if qty < min_size - EPSILON:
        return "probe_qty_below_min_size"
    if allow_min_size_edge_test:
        return None
    if qty + EPSILON < (min_size * multiplier):
        return "probe_qty_below_realistic_threshold"
    return None


def validate_runner_probe_sizes(
    original_base_qty: float,
    current_qty: float,
    runner_pct: float,
    min_size: float,
    *,
    preferred_multiplier: float = 2.0,
) -> tuple[float, float, Optional[str]]:
    if runner_pct <= 0.0 or runner_pct >= 1.0:
        return 0.0, 0.0, "invalid_runner_pct"
    if original_base_qty <= EPSILON:
        return 0.0, 0.0, "original_base_qty_missing_for_runner"
    runner_target_qty = original_base_qty * runner_pct
    reduce_qty = current_qty - runner_target_qty
    if reduce_qty <= EPSILON:
        return runner_target_qty, reduce_qty, "target_runner_no_reduce_needed"
    if runner_target_qty + EPSILON < min_size:
        return runner_target_qty, reduce_qty, "target_runner_qty_below_min_size"
    if reduce_qty + EPSILON < min_size:
        return runner_target_qty, reduce_qty, "target_runner_reduce_qty_below_min_size"
    preferred = min_size * preferred_multiplier
    if runner_target_qty + EPSILON < preferred:
        return runner_target_qty, reduce_qty, "target_runner_qty_below_realistic_threshold"
    if reduce_qty + EPSILON < preferred:
        return runner_target_qty, reduce_qty, "target_runner_reduce_qty_below_realistic_threshold"
    return runner_target_qty, reduce_qty, None
