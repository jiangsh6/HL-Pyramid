from __future__ import annotations

import json

from src.core.config_loader import load_config
from src.notifications.telegram import format_event
from src.reporting.strategy_sanity_audit import (
    build_strategy_sanity_audit,
    classify_parameter,
    interpret_parameter,
    recommendations_for,
    strategy_audit_event,
    write_strategy_sanity_audit,
)


TESTNET_WALLET = "0x1111111111111111111111111111111111111111"


def _cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    cfg = load_config("config/hype_testnet_soak.yaml")
    cfg.logging["run_dir"] = str(tmp_path)
    return cfg


def test_threshold_interpretation_for_pct_ratio_bps():
    interpreted, meaning = interpret_parameter("entry.starter.max_distance_above_ma20_pct", 1.0)
    assert interpreted == "100.0%"
    assert meaning == "fractional percent threshold"

    interpreted, meaning = interpret_parameter("entry.starter.min_volume_vs_20d_avg", 1.2)
    assert interpreted == "1.20x"
    assert meaning == "volume ratio threshold"

    interpreted, meaning = interpret_parameter("risk.no_trade_conditions.max_spread_bps", 50)
    assert interpreted == "50.0 bps"
    assert meaning == "basis-point threshold"


def test_warning_classification_for_permissive_and_strict_values():
    assessment, note = classify_parameter("entry.starter.max_distance_above_ma20_pct", 1.0)
    assert assessment == "UNUSUALLY_PERMISSIVE"
    assert "moving average" in note

    assessment, note = classify_parameter("entry.starter.min_volume_vs_20d_avg", 0.0)
    assert assessment == "UNUSUALLY_PERMISSIVE"
    assert "disabled" in note

    assessment, note = classify_parameter("add.max_add_count", 0)
    assert assessment == "UNUSUALLY_STRICT"
    assert "no pyramid adds" in note


def test_recommendation_generation():
    assert recommendations_for("entry.starter.max_distance_above_ma20_pct") == ("15%", "8%", "5%")
    assert recommendations_for("entry.starter.max_intraday_gain_pct") == ("8%", "5%", "3%")
    assert recommendations_for("entry.starter.min_volume_vs_20d_avg") == ("0.8x", "1.0x", "1.2x")


def test_strategy_sanity_json_output(monkeypatch, tmp_path):
    cfg = _cfg(monkeypatch, tmp_path)
    latest_signal = {
        "action": "buy_starter",
        "readiness_score": 100,
        "conditions": [
            {
                "condition": "distance_above_ma20_within_limit",
                "current_value": 0.102,
                "threshold": 1.0,
                "status": "PASS",
                "distance_pct": 0.0,
            }
        ],
    }

    audit = build_strategy_sanity_audit(cfg, latest_signal)
    path = write_strategy_sanity_audit(cfg, audit)
    raw = json.loads(path.read_text())

    assert raw["coin"] == "HYPE"
    assert raw["network"] == "testnet"
    assert raw["parameters_reviewed"] > 0
    assert raw["warning_count"] > 0
    assert any(row["parameter"] == "entry.starter.max_distance_above_ma20_pct" for row in raw["warnings"])
    assert raw["current_signal"]["action"] == "buy_starter"


def test_strategy_audit_telegram_summary(monkeypatch, tmp_path):
    cfg = _cfg(monkeypatch, tmp_path)
    audit = build_strategy_sanity_audit(cfg, {"action": "buy_starter", "readiness_score": 100})
    path = write_strategy_sanity_audit(cfg, audit)

    message = format_event(strategy_audit_event(cfg, audit, path))

    assert "🧪 HYPE Strategy Audit" in message
    assert "Parameters Reviewed" in message
    assert "Warnings" in message
    assert "Current Signal: buy_starter" in message
    assert "Audit Report: strategy_sanity_audit.json" in message
