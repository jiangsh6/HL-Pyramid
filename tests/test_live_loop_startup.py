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
    next_4h_candle_close,
    run_intraday_monitor,
    startup_checks,
    _install_shutdown_handler,
)
from src.core.config_loader import load_config
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
