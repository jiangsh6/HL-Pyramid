from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import scripts.run_daily_decision as daily_script
import scripts.run_live_hl as live_script
from scripts.run_live_hl import EXECUTION_ERROR, HALTED, CONTINUE
from src.core.models import ActionType, BotState, Decision
from src.execution.hl_broker import HLExecutionError
from src.reporting.state_writer import read_state
from tests._helpers import base_config, make_indicators, make_state


def _write_temp_config(src: str, dst: Path, run_dir: Path) -> Path:
    text = Path(src).read_text()
    text = text.replace("run_dir: reports/long_thesis_bot", f"run_dir: {run_dir.as_posix()}")
    text = text.replace("run_dir: reports/btc_hl_testnet", f"run_dir: {run_dir.as_posix()}")
    dst.write_text(text)
    return dst


def test_daily_decision_corrupted_state_writes_halted_and_exits(tmp_path):
    run_dir = tmp_path / "reports"
    run_dir.mkdir()
    state_path = run_dir / "state.json"
    state_path.write_text("{bad-json")
    cfg_path = _write_temp_config(
        "config/mu_long_thesis.yaml", tmp_path / "mu.yaml", run_dir
    )

    code = daily_script.main([
        "--config", str(cfg_path),
        "--data-csv", "tests/fixtures/mu_sample.csv",
    ])

    assert code == 1
    state = read_state(str(state_path))
    assert state.state == BotState.HALTED
    assert state.halted is True
    assert state.halt_reason == "state_load_or_validation_error"


def test_live_hl_state_load_failure_persists_halted_and_does_not_start_feed(tmp_path, monkeypatch):
    run_dir = tmp_path / "reports"
    run_dir.mkdir()
    state_path = run_dir / "state.json"
    state_path.write_text("{bad-json")
    cfg_path = _write_temp_config(
        "config/btc_long_thesis.yaml", tmp_path / "btc.yaml", run_dir
    )

    monkeypatch.setattr(live_script, "startup_checks", lambda config: "0x" + "a" * 64)
    monkeypatch.setattr(live_script, "HyperliquidClient", lambda network: object())
    feed_cls = MagicMock()
    monkeypatch.setattr(live_script, "HLWebSocketFeed", feed_cls)
    live_script._STOP_EVENT.clear()

    with monkeypatch.context() as m:
        m.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0x1111111111111111111111111111111111111111")
        live_script.main(str(cfg_path), dry_run=True)

    assert live_script._STOP_EVENT.is_set()
    feed_cls.assert_not_called()
    state = read_state(str(state_path))
    assert state.state == BotState.HALTED
    assert state.halt_reason == "state_load_or_validation_error"
    live_script._STOP_EVENT.clear()


def _patch_cycle_common(monkeypatch, decision: Decision) -> None:
    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *args, **kwargs: object())
    monkeypatch.setattr(live_script, "calc_indicators", lambda *args, **kwargs: make_indicators())
    monkeypatch.setattr(live_script, "get_account_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(live_script, "engine_run", lambda *args, **kwargs: decision)
    monkeypatch.setattr(live_script, "apply_candle_close_funding", lambda state, *args, **kwargs: state)


def test_hl_execution_error_persists_halted_and_sets_stop_event(tmp_path, monkeypatch):
    cfg = base_config()
    cfg.hl = {"coin": "BTC", "bar_interval": "4h"}
    cfg.logging["run_dir"] = str(tmp_path)
    state = make_state(state=BotState.BASE_LONG)
    state_path = tmp_path / "state.json"
    decision = Decision(action=ActionType.BUY_ADDON, qty=0.001, reason="test")
    _patch_cycle_common(monkeypatch, decision)
    monkeypatch.setattr(
        live_script,
        "_dispatch_broker",
        lambda *args, **kwargs: (_ for _ in ()).throw(HLExecutionError("boom")),
    )
    live_script._STOP_EVENT.clear()

    status = live_script.run_decision_cycle(
        cfg, state, state_path, MagicMock(), "0xwallet", "0xkey"
    )

    assert status == EXECUTION_ERROR
    assert live_script._STOP_EVENT.is_set()
    persisted = read_state(str(state_path))
    assert persisted.state == BotState.HALTED
    assert persisted.halted is True
    assert persisted.halt_reason.startswith("hl_execution_error:boom")
    for fname in ("orders.csv", "positions.csv", "risk.csv", "decisions.csv"):
        assert (tmp_path / fname).exists()
    orders = (tmp_path / "orders.csv").read_text()
    assert "failed" in orders
    assert "0.001" in orders
    assert "hl_execution_error:boom" in orders
    assert "buy_addon" in (tmp_path / "decisions.csv").read_text()
    assert "halted" in (tmp_path / "risk.csv").read_text()
    live_script._STOP_EVENT.clear()


def test_halt_decision_persists_halted_and_skips_execution(tmp_path, monkeypatch):
    cfg = base_config()
    cfg.hl = {"coin": "BTC", "bar_interval": "4h"}
    cfg.logging["run_dir"] = str(tmp_path)
    state = make_state(state=BotState.BASE_LONG)
    state_path = tmp_path / "state.json"
    decision = Decision(
        action=ActionType.HALT,
        qty=0.0,
        reason="risk_halt",
        new_state=BotState.HALTED,
    )
    _patch_cycle_common(monkeypatch, decision)
    dispatch = MagicMock()
    monkeypatch.setattr(live_script, "_dispatch_broker", dispatch)
    live_script._STOP_EVENT.clear()

    status = live_script.run_decision_cycle(
        cfg, state, state_path, MagicMock(), "0xwallet", "0xkey"
    )

    assert status == HALTED
    assert live_script._STOP_EVENT.is_set()
    dispatch.assert_not_called()
    persisted = read_state(str(state_path))
    assert persisted.state == BotState.HALTED
    assert persisted.halted is True
    assert persisted.halt_reason == "risk_halt"
    for fname in ("orders.csv", "positions.csv", "risk.csv", "decisions.csv"):
        assert (tmp_path / fname).exists()
    live_script._STOP_EVENT.clear()


def test_successful_cycle_returns_continue(tmp_path, monkeypatch):
    cfg = base_config()
    cfg.hl = {"coin": "BTC", "bar_interval": "4h"}
    state = make_state(state=BotState.FLAT)
    state_path = tmp_path / "state.json"
    decision = Decision(action=ActionType.NO_ACTION, qty=0.0, reason="none")
    _patch_cycle_common(monkeypatch, decision)
    live_script._STOP_EVENT.clear()

    status = live_script.run_decision_cycle(
        cfg, state, state_path, MagicMock(), "0xwallet", "0xkey"
    )

    assert status == CONTINUE
    persisted = json.loads(state_path.read_text())
    assert persisted["state"] == "FLAT"
