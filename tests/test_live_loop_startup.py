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
    next_4h_candle_close,
    run_intraday_monitor,
    startup_checks,
    _install_shutdown_handler,
)
from src.core.config_loader import load_config
from src.hl.ws_feed import HLWebSocketFeed

HL_CONFIG_PATH = "config/btc_long_thesis.yaml"
FAKE_KEY = "0x" + "a" * 64


# ── Named tests ───────────────────────────────────────────────────────────────

def test_startup_requires_private_key():
    """startup_checks raises SystemExit when HL_PRIVATE_KEY is not set."""
    config = load_config(HL_CONFIG_PATH)
    env = {k: v for k, v in os.environ.items() if k != "HL_PRIVATE_KEY"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit):
            startup_checks(config)


def test_startup_requires_hl_config():
    """startup_checks raises SystemExit when the 'hl' config block is absent."""
    config = load_config(HL_CONFIG_PATH)
    config.hl = None  # type: ignore[assignment]
    with patch.dict(os.environ, {"HL_PRIVATE_KEY": FAKE_KEY}):
        with pytest.raises(SystemExit):
            startup_checks(config)


def test_startup_rejects_mainnet_without_env_var():
    """
    Config with hl.network='mainnet' and HL_ALLOW_MAINNET unset raises ValueError
    during load_config (config_loader validation), not in startup_checks.
    """
    from src.core.config_loader import validate_config
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
    config = load_config(HL_CONFIG_PATH)
    with patch.dict(os.environ, {"HL_PRIVATE_KEY": FAKE_KEY}):
        key = startup_checks(config)
    assert key == FAKE_KEY
