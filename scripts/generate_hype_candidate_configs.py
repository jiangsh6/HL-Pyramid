"""Generate HYPE production-candidate parameter configs.

The generated files remain testnet configs. They are production-like parameter
candidates for review and soak testing, not mainnet deployment configs.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


BASE_CONFIG = Path("config/hype_testnet_soak.yaml")
REPORT_DIR = Path("reports/hype_hl_testnet_soak")
PROFILE_FILES = {
    "aggressive": Path("config/hype_candidate_aggressive.yaml"),
    "normal": Path("config/hype_candidate_normal.yaml"),
    "conservative": Path("config/hype_candidate_conservative.yaml"),
}


PROFILE_SETTINGS: dict[str, dict[str, Any]] = {
    "aggressive": {
        "score": 62,
        "entry": {
            "exposure_pct": 0.55,
            "require_close_above_ma20": True,
            "max_distance_above_ma10_pct": 0.15,
            "max_distance_above_ma20_pct": 0.15,
            "max_intraday_gain_pct": 0.08,
            "min_volume_vs_20d_avg": 0.80,
            "max_gap_up_pct": 0.06,
        },
        "add": {
            "enabled": True,
            "max_add_count": 4,
            "min_days_after_entry_before_first_add": 1,
            "min_days_between_adds": 1,
            "min_unrealized_profit_before_first_add_pct": 0.03,
            "add_trigger_pct": 0.05,
            "add_sizes_pct": [0.15, 0.12, 0.08, 0.05],
            "require_close_above_ma10": True,
            "require_ma10_above_ma20": True,
            "require_price_above_last_add_price": True,
            "forbid_add_if_intraday_gain_above_pct": 0.08,
            "forbid_add_if_distance_above_ma10_pct": 0.12,
            "forbid_add_if_distance_above_ma20_pct": 0.15,
        },
        "capital": {
            "max_total_capital_at_risk_pct": 0.05,
            "max_symbol_exposure_pct": 0.80,
            "max_initial_exposure_pct": 0.60,
            "max_starter_exposure_pct": 0.55,
            "max_addon_exposure_pct": 0.20,
        },
    },
    "normal": {
        "score": 84,
        "entry": {
            "exposure_pct": 0.40,
            "require_close_above_ma20": True,
            "max_distance_above_ma10_pct": 0.08,
            "max_distance_above_ma20_pct": 0.08,
            "max_intraday_gain_pct": 0.05,
            "min_volume_vs_20d_avg": 1.00,
            "max_gap_up_pct": 0.04,
        },
        "add": {
            "enabled": True,
            "max_add_count": 3,
            "min_days_after_entry_before_first_add": 1,
            "min_days_between_adds": 2,
            "min_unrealized_profit_before_first_add_pct": 0.05,
            "add_trigger_pct": 0.06,
            "add_sizes_pct": [0.10, 0.075, 0.05],
            "require_close_above_ma10": True,
            "require_ma10_above_ma20": True,
            "require_price_above_last_add_price": True,
            "forbid_add_if_intraday_gain_above_pct": 0.05,
            "forbid_add_if_distance_above_ma10_pct": 0.08,
            "forbid_add_if_distance_above_ma20_pct": 0.08,
        },
        "capital": {
            "max_total_capital_at_risk_pct": 0.03,
            "max_symbol_exposure_pct": 0.50,
            "max_initial_exposure_pct": 0.40,
            "max_starter_exposure_pct": 0.35,
            "max_addon_exposure_pct": 0.10,
        },
    },
    "conservative": {
        "score": 91,
        "entry": {
            "exposure_pct": 0.25,
            "require_close_above_ma20": True,
            "max_distance_above_ma10_pct": 0.05,
            "max_distance_above_ma20_pct": 0.05,
            "max_intraday_gain_pct": 0.03,
            "min_volume_vs_20d_avg": 1.20,
            "max_gap_up_pct": 0.02,
        },
        "add": {
            "enabled": True,
            "max_add_count": 2,
            "min_days_after_entry_before_first_add": 2,
            "min_days_between_adds": 3,
            "min_unrealized_profit_before_first_add_pct": 0.08,
            "add_trigger_pct": 0.08,
            "add_sizes_pct": [0.05, 0.03],
            "require_close_above_ma10": True,
            "require_ma10_above_ma20": True,
            "require_price_above_last_add_price": True,
            "forbid_add_if_intraday_gain_above_pct": 0.03,
            "forbid_add_if_distance_above_ma10_pct": 0.05,
            "forbid_add_if_distance_above_ma20_pct": 0.05,
        },
        "capital": {
            "max_total_capital_at_risk_pct": 0.015,
            "max_symbol_exposure_pct": 0.30,
            "max_initial_exposure_pct": 0.25,
            "max_starter_exposure_pct": 0.25,
            "max_addon_exposure_pct": 0.05,
        },
    },
}


MATRIX_PARAMETERS = [
    "entry.starter.exposure_pct",
    "entry.starter.require_close_above_ma20",
    "entry.starter.max_distance_above_ma10_pct",
    "entry.starter.max_distance_above_ma20_pct",
    "entry.starter.max_intraday_gain_pct",
    "entry.starter.min_volume_vs_20d_avg",
    "entry.starter.max_gap_up_pct",
    "add.enabled",
    "add.max_add_count",
    "add.min_days_after_entry_before_first_add",
    "add.min_days_between_adds",
    "add.min_unrealized_profit_before_first_add_pct",
    "add.add_trigger_pct",
    "add.add_sizes_pct",
    "add.require_close_above_ma10",
    "add.require_ma10_above_ma20",
    "add.require_price_above_last_add_price",
    "add.forbid_add_if_intraday_gain_above_pct",
    "add.forbid_add_if_distance_above_ma10_pct",
    "add.forbid_add_if_distance_above_ma20_pct",
    "capital.max_symbol_exposure_pct",
    "capital.max_initial_exposure_pct",
    "capital.max_starter_exposure_pct",
    "capital.max_addon_exposure_pct",
    "capital.max_total_capital_at_risk_pct",
]


def _get(config: Mapping[str, Any], dotted: str) -> Any:
    current: Any = config
    for part in dotted.split("."):
        current = current[part]
    return current


def _set(config: dict[str, Any], dotted: str, value: Any) -> None:
    current: Any = config
    parts = dotted.split(".")
    for part in parts[:-1]:
        current = current[part]
    current[parts[-1]] = value


def apply_profile(base: Mapping[str, Any], profile: str) -> dict[str, Any]:
    cfg = copy.deepcopy(dict(base))
    settings = PROFILE_SETTINGS[profile]
    cfg["bot"]["name"] = f"hype_candidate_{profile}"
    cfg["thesis"]["thesis_name"] = f"HYPE {profile} production candidate"

    for key, value in settings["entry"].items():
        _set(cfg, f"entry.starter.{key}", value)
    for key, value in settings["add"].items():
        _set(cfg, f"add.{key}", value)
    for key, value in settings["capital"].items():
        _set(cfg, f"capital.{key}", value)

    # Keep generated candidates explicitly testnet-only.
    cfg["bot"]["mode"] = "testnet"
    cfg["hl"]["network"] = "testnet"
    cfg["hl"]["coin"] = "HYPE"
    cfg["symbol"]["ticker"] = "HYPE"
    return cfg


def build_matrix(base: Mapping[str, Any], candidates: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for parameter in MATRIX_PARAMETERS:
        rows.append({
            "parameter": parameter,
            "current_soak": _get(base, parameter),
            "aggressive": _get(candidates["aggressive"], parameter),
            "normal": _get(candidates["normal"], parameter),
            "conservative": _get(candidates["conservative"], parameter),
        })
    return rows


def production_scores() -> dict[str, int]:
    return {profile: int(settings["score"]) for profile, settings in PROFILE_SETTINGS.items()}


def generate(
    *,
    base_path: Path = BASE_CONFIG,
    output_files: Mapping[str, Path] = PROFILE_FILES,
    report_dir: Path = REPORT_DIR,
) -> dict[str, Any]:
    base = yaml.safe_load(base_path.read_text())
    candidates = {profile: apply_profile(base, profile) for profile in PROFILE_SETTINGS}

    for profile, path in output_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(candidates[profile], sort_keys=False))

    report_dir.mkdir(parents=True, exist_ok=True)
    matrix = build_matrix(base, candidates)
    matrix_csv = report_dir / "hype_parameter_matrix.csv"
    matrix_json = report_dir / "hype_parameter_matrix.json"

    with matrix_csv.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["parameter", "current_soak", "aggressive", "normal", "conservative"],
        )
        writer.writeheader()
        writer.writerows(matrix)

    payload = {
        "base_config": str(base_path),
        "generated_configs": {profile: str(path) for profile, path in output_files.items()},
        "production_readiness_scores": production_scores(),
        "matrix": matrix,
    }
    matrix_json.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate HYPE production candidate configs")
    parser.add_argument("--base-config", default=str(BASE_CONFIG))
    parser.add_argument("--report-dir", default=str(REPORT_DIR))
    args = parser.parse_args(argv)

    payload = generate(base_path=Path(args.base_config), report_dir=Path(args.report_dir))
    print("HYPE_CANDIDATE_CONFIGS_GENERATED")
    for profile, path in payload["generated_configs"].items():
        print(f"{profile}_config={path}")
    print("matrix_csv=" + str(Path(args.report_dir) / "hype_parameter_matrix.csv"))
    print("matrix_json=" + str(Path(args.report_dir) / "hype_parameter_matrix.json"))
    print("scores=" + json.dumps(payload["production_readiness_scores"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
