from __future__ import annotations

import csv
import json
from pathlib import Path

import yaml

from scripts.generate_hype_candidate_configs import generate, production_scores
from src.core.config_loader import load_config


TESTNET_WALLET = "0x1111111111111111111111111111111111111111"


def test_candidate_configs_generated_and_load_successfully(monkeypatch, tmp_path):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    outputs = {
        "aggressive": tmp_path / "hype_candidate_aggressive.yaml",
        "normal": tmp_path / "hype_candidate_normal.yaml",
        "conservative": tmp_path / "hype_candidate_conservative.yaml",
    }

    generate(output_files=outputs, report_dir=tmp_path / "reports")

    for profile, path in outputs.items():
        assert path.exists()
        cfg = load_config(str(path))
        assert cfg.hl["network"] == "testnet"
        assert cfg.hl["coin"] == "HYPE"
        assert cfg.symbol["ticker"] == "HYPE"
        assert cfg.add["max_add_count"] == len(cfg.add["add_sizes_pct"])
        assert profile in cfg.bot["name"]


def test_production_scores_generated():
    scores = production_scores()

    assert scores == {
        "aggressive": 62,
        "normal": 84,
        "conservative": 91,
    }


def test_matrix_outputs_generated(monkeypatch, tmp_path):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    outputs = {
        "aggressive": tmp_path / "aggressive.yaml",
        "normal": tmp_path / "normal.yaml",
        "conservative": tmp_path / "conservative.yaml",
    }

    payload = generate(output_files=outputs, report_dir=tmp_path)

    csv_path = tmp_path / "hype_parameter_matrix.csv"
    json_path = tmp_path / "hype_parameter_matrix.json"
    assert csv_path.exists()
    assert json_path.exists()

    rows = list(csv.DictReader(csv_path.open()))
    assert any(row["parameter"] == "entry.starter.max_intraday_gain_pct" for row in rows)
    assert any(row["parameter"] == "capital.max_total_capital_at_risk_pct" for row in rows)

    raw = json.loads(json_path.read_text())
    assert raw["production_readiness_scores"] == production_scores()
    assert raw["matrix"] == payload["matrix"]


def test_original_hype_soak_config_unchanged(tmp_path):
    source = Path("config/hype_testnet_soak.yaml")
    before = source.read_text()
    outputs = {
        "aggressive": tmp_path / "aggressive.yaml",
        "normal": tmp_path / "normal.yaml",
        "conservative": tmp_path / "conservative.yaml",
    }

    generate(output_files=outputs, report_dir=tmp_path)

    assert source.read_text() == before


def test_candidate_profiles_have_expected_parameter_shape(tmp_path):
    outputs = {
        "aggressive": tmp_path / "aggressive.yaml",
        "normal": tmp_path / "normal.yaml",
        "conservative": tmp_path / "conservative.yaml",
    }

    generate(output_files=outputs, report_dir=tmp_path)
    aggressive = yaml.safe_load(outputs["aggressive"].read_text())
    normal = yaml.safe_load(outputs["normal"].read_text())
    conservative = yaml.safe_load(outputs["conservative"].read_text())

    assert aggressive["entry"]["starter"]["max_intraday_gain_pct"] > normal["entry"]["starter"]["max_intraday_gain_pct"]
    assert normal["entry"]["starter"]["max_intraday_gain_pct"] > conservative["entry"]["starter"]["max_intraday_gain_pct"]
    assert aggressive["capital"]["max_symbol_exposure_pct"] > normal["capital"]["max_symbol_exposure_pct"]
    assert normal["capital"]["max_symbol_exposure_pct"] > conservative["capital"]["max_symbol_exposure_pct"]
    assert aggressive["add"]["max_add_count"] > normal["add"]["max_add_count"]
    assert normal["add"]["max_add_count"] > conservative["add"]["max_add_count"]
