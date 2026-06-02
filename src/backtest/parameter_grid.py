from __future__ import annotations

import copy
import itertools
from typing import Any, Iterable, Mapping

from src.core.models import BotConfig


DEFAULT_HYPE_GRID = {
    "entry.starter.max_distance_above_ma20_pct": [0.05, 0.08, 0.12],
    "entry.starter.max_intraday_gain_pct": [0.03, 0.05, 0.08],
    "entry.starter.min_volume_vs_20d_avg": [0.8, 1.0, 1.2],
    "add.add_trigger_pct": [0.04, 0.06, 0.08, 0.10],
    "add.max_add_count": [2, 3, 4],
    "capital.max_total_capital_at_risk_pct": [0.015, 0.03, 0.05],
}


def set_dotted(obj: dict[str, Any], dotted: str, value: Any) -> None:
    cur: Any = obj
    parts = dotted.split(".")
    for part in parts[:-1]:
        cur = cur[part]
    cur[parts[-1]] = value


def config_to_raw(config: BotConfig) -> dict[str, Any]:
    return config.model_dump(mode="python")


def parameter_combinations(grid: Mapping[str, Iterable[Any]] = DEFAULT_HYPE_GRID) -> list[dict[str, Any]]:
    keys = list(grid.keys())
    values = [list(grid[key]) for key in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def apply_parameter_set(raw_config: dict[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(raw_config)
    for key, value in params.items():
        set_dotted(out, key, value)
    if "add.max_add_count" in params:
        count = int(params["add.max_add_count"])
        base_sizes = [0.10, 0.075, 0.05, 0.03]
        out["add"]["add_sizes_pct"] = base_sizes[:count]
    return out
