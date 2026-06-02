from __future__ import annotations

import csv
import json
from datetime import date

from src.core.config_loader import load_config
from src.core.models import ActionType, BotState, Decision, IndicatorSnapshot, ThesisState
from src.notifications.telegram import format_event
from src.reporting.signal_diagnostics import (
    build_signal_diagnostics,
    closest_trigger,
    evaluate_entry_conditions,
    format_signal_check_event,
    readiness_score,
    write_signal_diagnostics,
)


TESTNET_WALLET = "0x1111111111111111111111111111111111111111"


def _cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", TESTNET_WALLET)
    cfg = load_config("config/hype_testnet_soak.yaml")
    cfg.logging["run_dir"] = str(tmp_path)
    return cfg


def _cfg_with_close_filter(monkeypatch, tmp_path):
    cfg = _cfg(monkeypatch, tmp_path)
    cfg.entry["starter"]["require_close_above_ma20"] = True
    cfg.entry["starter"]["min_volume_vs_20d_avg"] = 0.8
    return cfg


def _indicators(**overrides) -> IndicatorSnapshot:
    values = {
        "date": date(2026, 6, 1),
        "adj_close": 18.42,
        "open": 18.3,
        "high": 18.5,
        "low": 18.1,
        "volume": 1210.0,
        "prev_adj_close": 18.2,
        "ma5": 18.2,
        "ma10": 18.1,
        "ma20": 18.70,
        "ma50": 17.9,
        "atr14": 0.42,
        "avg_volume_20d": 1000.0,
        "prior_highest_high_20d": 19.0,
        "drawdown_from_20d_high": 0.04,
        "distance_from_ma10": 0.02,
        "distance_from_ma20": -0.015,
        "intraday_return": 0.01,
        "gap_up_pct": 0.0,
        "gap_down_pct": 0.0,
        "close": 18.42,
    }
    values.update(overrides)
    return IndicatorSnapshot(**values)


def test_signal_diagnostics_csv_row_and_json_snapshot(monkeypatch, tmp_path):
    cfg = _cfg_with_close_filter(monkeypatch, tmp_path)
    state = ThesisState(symbol="HYPE", state=BotState.FLAT)
    decision = Decision(action=ActionType.NO_ACTION, reason="no_trigger", blockers=[])

    diagnostics = build_signal_diagnostics(
        run_id="run-1",
        coin="HYPE",
        network="testnet",
        state=state,
        decision=decision,
        indicators=_indicators(),
        config=cfg,
        exchange_position_qty=0.0,
        collateral_available=900.0,
        trading_collateral_available=True,
    )
    write_signal_diagnostics(str(tmp_path), diagnostics)

    csv_path = tmp_path / "signal_diagnostics.csv"
    rows = list(csv.DictReader(csv_path.open()))
    assert len(rows) == 1
    assert rows[0]["coin"] == "HYPE"
    assert rows[0]["action"] == "no_action"
    assert rows[0]["closest_trigger"] == "close_above_ma20"
    assert rows[0]["blocker_count"] == "1"
    assert "close_above_ma20" in rows[0]["condition_matrix_json"]

    snapshot = json.loads((tmp_path / "latest_signal_diagnostics.json").read_text())
    assert snapshot["readiness_score"] > 0
    assert snapshot["blockers"] == ["close_above_ma20"]
    assert snapshot["market"]["close"] == 18.42
    assert snapshot["indicators"]["atr"] == 0.42
    assert snapshot["risk"]["max_risk"] == 27.0


def test_readiness_score_closest_trigger_and_blocker_count(monkeypatch, tmp_path):
    cfg = _cfg_with_close_filter(monkeypatch, tmp_path)
    state = ThesisState(symbol="HYPE", state=BotState.FLAT)
    decision = Decision(action=ActionType.NO_ACTION, reason="no_trigger", blockers=["risk_gate"])

    diagnostics = build_signal_diagnostics(
        run_id="run-2",
        coin="HYPE",
        network="testnet",
        state=state,
        decision=decision,
        indicators=_indicators(),
        config=cfg,
        trading_collateral_available=True,
    )

    assert 0 < diagnostics["readiness_score"] < 100
    assert diagnostics["closest_trigger"] == "close_above_ma20"
    assert diagnostics["closest_trigger_distance"].endswith("%")
    assert diagnostics["blocker_count"] == 2
    assert diagnostics["no_action_diagnostic"] == "Only blocker: close_above_ma20. All other active conditions pass."


def test_condition_status_pass_fail_skipped(monkeypatch, tmp_path):
    cfg = _cfg_with_close_filter(monkeypatch, tmp_path)
    flat = ThesisState(symbol="HYPE", state=BotState.FLAT)
    active = evaluate_entry_conditions(flat, _indicators(), cfg, collateral_available=True)
    statuses = {condition.condition: condition.status for condition in active}

    assert statuses["thesis_enabled"] == "PASS"
    assert statuses["close_above_ma20"] == "FAIL"
    assert statuses["volume_vs_20d_avg"] == "PASS"

    long_state = ThesisState(symbol="HYPE", state=BotState.BASE_LONG)
    skipped = evaluate_entry_conditions(long_state, _indicators(), cfg)
    assert {condition.condition: condition.status for condition in skipped}["starter_state_flat"] == "SKIPPED"


def test_readiness_and_closest_trigger_helpers(monkeypatch, tmp_path):
    cfg = _cfg_with_close_filter(monkeypatch, tmp_path)
    conditions = [
        *evaluate_entry_conditions(
            ThesisState(symbol="HYPE", state=BotState.FLAT),
            _indicators(adj_close=18.8, ma20=18.7, distance_from_ma20=0.005),
            cfg,
            collateral_available=True,
        )
    ]

    assert readiness_score(conditions) == 100
    assert closest_trigger(conditions) == ("", "")


def test_signal_check_telegram_summary_uses_mobile_format(monkeypatch, tmp_path):
    cfg = _cfg_with_close_filter(monkeypatch, tmp_path)
    diagnostics = build_signal_diagnostics(
        run_id="run-3",
        coin="HYPE",
        network="testnet",
        state=ThesisState(symbol="HYPE", state=BotState.FLAT),
        decision=Decision(action=ActionType.NO_ACTION, reason="no_trigger"),
        indicators=_indicators(),
        config=cfg,
    )

    message = format_event(format_signal_check_event(diagnostics))

    assert "🧭 HYPE Signal Check" in message
    assert "Readiness:" in message
    assert "Closest Trigger:" in message
    assert "❌ close_above_ma20" in message
    assert "Next Check:\n1h" in message


def test_signal_check_telegram_unknown_fields_render_dash():
    message = format_event({"type": "signal_check", "coin": "HYPE"})

    assert "unknown" not in message.lower()
    assert "Needs -" in message
