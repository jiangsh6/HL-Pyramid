"""
HL Phase 5 — WebSocket feed tests.

All 8 named tests:
  test_ws_feed_start_creates_thread
  test_ws_feed_stop_sets_event
  test_ws_feed_get_latest_price_returns_none_when_empty
  test_ws_feed_get_latest_price_returns_value_after_message
  test_ws_feed_parse_price_extracts_correct_coin
  test_ws_feed_parse_price_returns_none_for_missing_coin
  test_ws_feed_parse_price_handles_invalid_json
  test_ws_feed_max_reconnects_puts_none_sentinel
"""
from __future__ import annotations

import json
import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.hl.ws_feed import TESTNET_WS_URL, HLWebSocketFeed


def _make_feed(coin: str = "BTC") -> HLWebSocketFeed:
    return HLWebSocketFeed(ws_url=TESTNET_WS_URL, coin=coin)


def _all_mids_msg(prices: dict) -> str:
    return json.dumps({"channel": "allMids", "data": {"mids": {k: str(v) for k, v in prices.items()}}})


# ── Named tests ───────────────────────────────────────────────────────────────

def test_ws_feed_start_creates_thread():
    """start() creates and starts a daemon thread."""
    feed = _make_feed()
    assert feed._thread is None

    with patch.object(feed, "_run_loop"):  # prevent actual WS connection
        feed.start()

    assert feed._thread is not None
    assert feed._thread.daemon is True
    feed.stop()


def test_ws_feed_stop_sets_event():
    """stop() sets the internal stop event."""
    feed = _make_feed()
    assert not feed._stop_event.is_set()

    with patch.object(feed, "_run_loop"):
        feed.start()

    feed.stop()
    assert feed._stop_event.is_set()


def test_ws_feed_get_latest_price_returns_none_when_empty():
    """get_latest_price() returns None when no price has been received."""
    feed = _make_feed()
    assert feed.get_latest_price() is None


def test_ws_feed_get_latest_price_returns_value_after_message():
    """get_latest_price() returns the latest price placed on the buffer."""
    feed = _make_feed()
    feed._price_buffer.put(51000.0)
    feed._price_buffer.put(51100.0)
    price = feed.get_latest_price()
    assert price == pytest.approx(51100.0)
    # Buffer should be drained
    assert feed.get_latest_price() is None


def test_ws_feed_parse_price_extracts_correct_coin():
    """_parse_price extracts the price for the configured coin."""
    feed = _make_feed("BTC")
    msg = _all_mids_msg({"BTC": 50000.0, "ETH": 3000.0})
    assert feed._parse_price(msg) == pytest.approx(50000.0)


def test_ws_feed_parse_price_returns_none_for_missing_coin():
    """_parse_price returns None when the coin is absent from mids."""
    feed = _make_feed("SOL")
    msg = _all_mids_msg({"BTC": 50000.0, "ETH": 3000.0})
    assert feed._parse_price(msg) is None


def test_ws_feed_parse_price_handles_invalid_json():
    """_parse_price returns None for malformed JSON without raising."""
    feed = _make_feed()
    assert feed._parse_price("not-json") is None
    assert feed._parse_price("{bad json") is None


def test_ws_feed_max_reconnects_puts_none_sentinel():
    """
    After MAX_RECONNECTS failures _run_loop puts None on the buffer
    so the caller knows to halt.
    """
    feed = _make_feed()
    feed._reconnect_attempts = feed.MAX_RECONNECTS  # already at limit

    call_count = 0

    def _fail_connect():
        nonlocal call_count
        call_count += 1
        raise ConnectionRefusedError("refused")

    with patch.object(feed, "_connect_and_run", side_effect=_fail_connect):
        feed._run_loop()

    # Should have put None sentinel
    sentinel = feed._price_buffer.get_nowait()
    assert sentinel is None
    assert call_count == 1  # tried once then gave up


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_ws_feed_parse_price_non_allmids_channel_ignored():
    """Messages on other channels are ignored (returns None)."""
    feed = _make_feed()
    msg = json.dumps({"channel": "trades", "data": {"BTC": "50000"}})
    assert feed._parse_price(msg) is None


def test_ws_feed_buffer_overflow_drops_oldest():
    """When the buffer is full, the oldest price is dropped to make room."""
    feed = _make_feed()
    # Fill buffer to capacity
    for i in range(100):
        feed._price_buffer.put(float(i))

    # _put_price should not block or raise even when full
    feed._put_price(9999.0)

    # Drain and confirm 9999 is present
    prices = []
    try:
        while True:
            prices.append(feed._price_buffer.get_nowait())
    except queue.Empty:
        pass

    assert 9999.0 in prices
    assert len(prices) == 100
