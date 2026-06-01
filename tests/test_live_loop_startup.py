"""
HL Phase 5 — Live loop startup and utility tests.

All 6 named tests:
  test_startup_requires_private_key
  test_startup_requires_hl_config
  test_startup_rejects_mainnet_without_env_var
  test_shutdown_handler_sets_stop_event
  test_next_4h_candle_close_is_future
  test_intraday_monitor_respects_stop_event
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from scripts.run_live_hl import (
    CONTINUE,
    STOPPED,
    emit_heartbeat,
    next_4h_candle_close,
    run_recurring_loop,
    run_intraday_monitor,
    startup_checks,
    _install_shutdown_handler,
)
from src.core.config_loader import load_config
from src.core.models import PendingOrder, ThesisState
from src.hl.ws_feed import HLWebSocketFeed

HL_CONFIG_PATH = "config/btc_long_thesis.yaml"
FAKE_KEY = "0x" + "a" * 64
TESTNET_WALLET = "0x1111111111111111111111111111111111111111"


# ── Named tests ───────────────────────────────────────────────────────────────

def test_startup_requires_private_key():
    """startup_checks raises SystemExit when the testnet key env is not set."""
    with patch.dict(os.environ, {"HL_TESTNET_ACCOUNT_ADDRESS": TESTNET_WALLET}, clear=False):
        config = load_config(HL_CONFIG_PATH)
    env = {k: v for k, v in os.environ.items() if k != "HL_TESTNET_AGENT_PRIVATE_KEY"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit):
            startup_checks(config)


def test_startup_requires_hl_config():
    """startup_checks raises SystemExit when the 'hl' config block is absent."""
    with patch.dict(os.environ, {"HL_TESTNET_ACCOUNT_ADDRESS": TESTNET_WALLET}, clear=False):
        config = load_config(HL_CONFIG_PATH)
    config.hl = None  # type: ignore[assignment]
    with patch.dict(os.environ, {
        "HL_TESTNET_AGENT_PRIVATE_KEY": FAKE_KEY,
        "HL_TESTNET_ACCOUNT_ADDRESS": TESTNET_WALLET,
    }):
        with pytest.raises(SystemExit):
            startup_checks(config)


def test_startup_rejects_mainnet_without_env_var():
    """
    Config with hl.network='mainnet' and HL_ALLOW_MAINNET unset raises ValueError
    during load_config (config_loader validation), not in startup_checks.
    """
    from src.core.config_loader import validate_config
    with patch.dict(os.environ, {"HL_TESTNET_ACCOUNT_ADDRESS": TESTNET_WALLET}, clear=False):
        config = load_config(HL_CONFIG_PATH)
    config.hl["network"] = "mainnet"  # type: ignore[index]
    env = {k: v for k, v in os.environ.items() if k != "HL_ALLOW_MAINNET"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="HL_ALLOW_MAINNET"):
            validate_config(config)


def test_shutdown_handler_sets_stop_event():
    """_install_shutdown_handler wires SIGINT so that the stop event is set on signal."""
    import signal

    stop_event = threading.Event()
    _install_shutdown_handler(stop_event)

    # Simulate the signal by invoking the handler directly
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)
    handler(signal.SIGINT, None)

    assert stop_event.is_set()


def test_next_4h_candle_close_is_future():
    """next_4h_candle_close() always returns a time strictly after now."""
    now  = datetime.now(timezone.utc)
    nxt  = next_4h_candle_close(now)
    assert nxt > now
    # Must land on a 4h UTC boundary (seconds since epoch divisible by 4*3600)
    assert int(nxt.timestamp()) % (4 * 3600) == 0


def test_intraday_monitor_respects_stop_event():
    """run_intraday_monitor exits promptly when stop_event is set."""
    feed       = MagicMock(spec=HLWebSocketFeed)
    feed.get_latest_price.return_value = None
    stop_event = threading.Event()

    thread = threading.Thread(
        target=run_intraday_monitor,
        args=(feed, stop_event),
        kwargs={"check_interval_seconds": 1},
        daemon=True,
    )
    thread.start()

    time.sleep(0.1)
    stop_event.set()
    thread.join(timeout=3)
    assert not thread.is_alive()


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_next_4h_candle_close_boundary_values():
    """next_4h_candle_close at exactly a boundary returns the *next* boundary."""
    # Exactly at 00:00 UTC on any day is a boundary — next one is 04:00 UTC
    exactly_midnight = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    nxt = next_4h_candle_close(exactly_midnight)
    assert nxt > exactly_midnight
    assert int(nxt.timestamp()) % (4 * 3600) == 0


def test_startup_checks_returns_key_when_valid():
    """startup_checks returns the private key string on success."""
    with patch.dict(os.environ, {"HL_TESTNET_ACCOUNT_ADDRESS": TESTNET_WALLET}, clear=False):
        config = load_config(HL_CONFIG_PATH)
    with patch.dict(os.environ, {
        "HL_TESTNET_AGENT_PRIVATE_KEY": FAKE_KEY,
        "HL_TESTNET_ACCOUNT_ADDRESS": TESTNET_WALLET,
    }):
        key = startup_checks(config)
    assert key == FAKE_KEY


def test_one_cycle_main_exits_without_starting_recurring_loop(monkeypatch, tmp_path):
    from scripts import run_live_hl as live_script

    cfg = load_config("config/mu_long_thesis.yaml")
    cfg.bot["mode"] = "testnet"
    cfg.data["source"] = "hyperliquid"
    cfg.logging["run_dir"] = str(tmp_path / "run")
    cfg.hl = {
        "network": "testnet",
        "coin": "BTC",
        "wallet_address": TESTNET_WALLET,
        "bar_interval": "4h",
        "sz_decimals": 5,
    }

    monkeypatch.setattr(live_script, "load_config", lambda _: cfg)
    monkeypatch.setattr(live_script, "startup_checks", lambda config: FAKE_KEY)
    monkeypatch.setattr(live_script, "HyperliquidClient", lambda network: object())
    monkeypatch.setattr(
        live_script,
        "load_state_or_halt",
        lambda *args, **kwargs: (live_script.ThesisState(symbol="BTC"), False),
    )
    cycle = {"count": 0}

    def fake_cycle(*args, **kwargs):
        cycle["count"] += 1
        return CONTINUE

    feed_cls = MagicMock()
    monkeypatch.setattr(live_script, "run_decision_cycle", fake_cycle)
    monkeypatch.setattr(live_script, "HLWebSocketFeed", feed_cls)
    live_script._STOP_EVENT.clear()

    live_script.main("unused.yaml", dry_run=False, one_cycle=True)

    assert cycle["count"] == 1
    feed_cls.assert_not_called()
    assert live_script._STOP_EVENT.is_set()
    live_script._STOP_EVENT.clear()


def test_main_uses_configured_recurring_loop(monkeypatch, tmp_path):
    from scripts import run_live_hl as live_script

    cfg = load_config("config/mu_long_thesis.yaml")
    cfg.bot["mode"] = "testnet"
    cfg.data["source"] = "hyperliquid"
    cfg.logging["run_dir"] = str(tmp_path / "run")
    cfg.execution["loop_interval_minutes"] = 0.01
    cfg.hl = {
        "network": "testnet",
        "coin": "BTC",
        "wallet_address": TESTNET_WALLET,
        "bar_interval": "4h",
        "sz_decimals": 5,
    }

    monkeypatch.setattr(live_script, "load_config", lambda _: cfg)
    monkeypatch.setattr(live_script, "startup_checks", lambda config: FAKE_KEY)
    monkeypatch.setattr(live_script, "HyperliquidClient", lambda network: object())
    monkeypatch.setattr(
        live_script,
        "load_state_or_halt",
        lambda *args, **kwargs: (live_script.ThesisState(symbol="BTC"), False),
    )
    feed = MagicMock()
    feed.get_latest_price.return_value = None
    monkeypatch.setattr(live_script, "HLWebSocketFeed", lambda *args, **kwargs: feed)
    loop = {"count": 0}

    def fake_loop(*args, **kwargs):
        loop["count"] += 1
        live_script._STOP_EVENT.set()
        return live_script.STOPPED

    monkeypatch.setattr(live_script, "run_recurring_loop", fake_loop)
    live_script._STOP_EVENT.clear()

    live_script.main("unused.yaml", dry_run=True)

    assert loop["count"] == 1
    feed.start.assert_called_once()
    feed.stop.assert_called_once()
    assert live_script._STOP_EVENT.is_set()
    live_script._STOP_EVENT.clear()


def _loop_cfg(tmp_path):
    cfg = load_config("config/mu_long_thesis.yaml")
    cfg.bot["mode"] = "testnet"
    cfg.data["source"] = "hyperliquid"
    cfg.logging["run_dir"] = str(tmp_path / "run")
    cfg.execution["loop_interval_minutes"] = 0.01
    cfg.hl = {
        "network": "testnet",
        "coin": "BTC",
        "wallet_address": TESTNET_WALLET,
        "bar_interval": "4h",
        "sz_decimals": 5,
    }
    return cfg


def test_heartbeat_contains_required_fields(caplog):
    from scripts import run_live_hl as live_script

    state = ThesisState(
        symbol="BTC",
        pending_order=PendingOrder(
            oid=123,
            symbol="BTC",
            action="sell_stop",
            side="sell",
            reduce_only=True,
            qty=0.005,
            qty_submitted=0.005,
            qty_filled=0.0,
            qty_remaining=0.005,
            limit_px=70000.0,
            status="submitted_unfilled",
            created_at=datetime.now(timezone.utc),
        ),
    )

    caplog.set_level("INFO", logger="run_live_hl")
    heartbeat = emit_heartbeat(
        state,
        open_orders_count=1,
        last_decision=live_script.BLOCKED_EXISTING_OPEN_ORDER,
    )

    assert heartbeat["timestamp"]
    assert heartbeat["state"] == "FLAT"
    assert heartbeat["position_qty"] == 0.0
    assert heartbeat["open_orders_count"] == 1
    assert heartbeat["pending_order_status"] == "submitted_unfilled"
    assert heartbeat["last_decision"] == live_script.BLOCKED_EXISTING_OPEN_ORDER
    assert "heartbeat timestamp=" in caplog.text
    assert "open_orders_count=1" in caplog.text


def test_recurring_loop_runs_single_cycle(monkeypatch, tmp_path):
    from scripts import run_live_hl as live_script

    cfg = _loop_cfg(tmp_path)
    state = ThesisState(symbol="BTC")
    state_path = tmp_path / "state.json"
    calls = {"count": 0}

    def fake_cycle(*args, **kwargs):
        calls["count"] += 1
        return CONTINUE

    monkeypatch.setattr(live_script, "run_decision_cycle", fake_cycle)
    monkeypatch.setattr(live_script, "_heartbeat_open_orders_count", lambda **kwargs: 0)
    stop_event = threading.Event()

    status = run_recurring_loop(
        cfg,
        state,
        state_path,
        MagicMock(),
        TESTNET_WALLET,
        FAKE_KEY,
        max_cycles=1,
        stop_event=stop_event,
        sleep_fn=lambda seconds: None,
    )

    assert status == CONTINUE
    assert calls["count"] == 1
    assert not stop_event.is_set()


def test_recurring_loop_runs_repeated_cycles(monkeypatch, tmp_path):
    from scripts import run_live_hl as live_script

    cfg = _loop_cfg(tmp_path)
    state = ThesisState(symbol="BTC")
    sleeps = []
    calls = {"count": 0}

    def fake_cycle(*args, **kwargs):
        calls["count"] += 1
        return CONTINUE

    monkeypatch.setattr(live_script, "run_decision_cycle", fake_cycle)
    monkeypatch.setattr(live_script, "_heartbeat_open_orders_count", lambda **kwargs: 0)

    status = run_recurring_loop(
        cfg,
        state,
        tmp_path / "state.json",
        MagicMock(),
        TESTNET_WALLET,
        FAKE_KEY,
        max_cycles=2,
        stop_event=threading.Event(),
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    assert status == CONTINUE
    assert calls["count"] == 2
    assert sleeps == [0.6]


def test_recurring_loop_shutdown_behavior(monkeypatch, tmp_path):
    from scripts import run_live_hl as live_script

    cfg = _loop_cfg(tmp_path)
    state = ThesisState(symbol="BTC")
    stop_event = threading.Event()
    calls = {"count": 0}

    def fake_cycle(*args, **kwargs):
        calls["count"] += 1
        return CONTINUE

    def stop_after_sleep(seconds):
        stop_event.set()

    monkeypatch.setattr(live_script, "run_decision_cycle", fake_cycle)
    monkeypatch.setattr(live_script, "_heartbeat_open_orders_count", lambda **kwargs: 0)

    status = run_recurring_loop(
        cfg,
        state,
        tmp_path / "state.json",
        MagicMock(),
        TESTNET_WALLET,
        FAKE_KEY,
        stop_event=stop_event,
        sleep_fn=stop_after_sleep,
    )

    assert status == STOPPED
    assert calls["count"] == 1


def test_recurring_loop_prevents_duplicate_active_loop(tmp_path):
    from scripts import run_live_hl as live_script

    cfg = _loop_cfg(tmp_path)
    acquired = live_script._LOOP_LOCK.acquire(blocking=False)
    assert acquired
    try:
        with pytest.raises(RuntimeError, match="recurring_loop_already_active"):
            run_recurring_loop(
                cfg,
                ThesisState(symbol="BTC"),
                tmp_path / "state.json",
                MagicMock(),
                TESTNET_WALLET,
                FAKE_KEY,
                max_cycles=1,
                stop_event=threading.Event(),
                sleep_fn=lambda seconds: None,
            )
    finally:
        live_script._LOOP_LOCK.release()
