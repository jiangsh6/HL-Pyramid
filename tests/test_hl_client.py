"""
HL Phase 1 — HyperliquidClient tests.

All 5 named tests:
  test_client_uses_testnet_url
  test_client_uses_mainnet_url_when_configured
  test_post_info_returns_parsed_dict
  test_retry_on_timeout
  test_retry_exhausted_raises
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest
import requests

from src.hl.client import HyperliquidClient, TESTNET_URL, MAINNET_URL


# ── URL selection ────────────────────────────────────────────────────────────

def test_client_uses_testnet_url():
    client = HyperliquidClient(network="testnet")
    assert client.base_url == TESTNET_URL
    assert "testnet" in client.base_url


def test_client_uses_mainnet_url_when_configured():
    client = HyperliquidClient(network="mainnet")
    assert client.base_url == MAINNET_URL
    assert "testnet" not in client.base_url


def test_invalid_network_raises():
    with pytest.raises(ValueError, match="network must be"):
        HyperliquidClient(network="live")


# ── post_info happy path ─────────────────────────────────────────────────────

def test_post_info_returns_parsed_dict():
    """Mock a single successful request; verify JSON body is returned."""
    client = HyperliquidClient(network="testnet")
    expected = {"universe": [{"name": "BTC", "szDecimals": 3}]}

    mock_resp = MagicMock()
    mock_resp.json.return_value = expected
    mock_resp.raise_for_status = MagicMock()

    with patch("src.hl.client.requests.post", return_value=mock_resp) as mock_post:
        result = client.post_info({"type": "meta"})

    assert result == expected
    mock_post.assert_called_once_with(
        f"{TESTNET_URL}/info",
        json={"type": "meta"},
        timeout=client._timeout,
        headers={"Content-Type": "application/json"},
    )


# ── retry logic ──────────────────────────────────────────────────────────────

def test_retry_on_timeout():
    """First 2 attempts raise Timeout; 3rd succeeds."""
    client = HyperliquidClient(network="testnet", max_retries=3)
    expected = {"data": "ok"}

    success_resp = MagicMock()
    success_resp.json.return_value = expected
    success_resp.raise_for_status = MagicMock()

    side_effects = [
        requests.exceptions.Timeout("attempt 1"),
        requests.exceptions.Timeout("attempt 2"),
        success_resp,
    ]

    with patch("src.hl.client.time.sleep"), \
         patch("src.hl.client.requests.post", side_effect=side_effects):
        result = client.post_info({"type": "meta"})

    assert result == expected


def test_retry_exhausted_raises():
    """All 3 attempts raise Timeout; the last exception must propagate."""
    client = HyperliquidClient(network="testnet", max_retries=3)

    with patch("src.hl.client.time.sleep"), \
         patch("src.hl.client.requests.post",
               side_effect=requests.exceptions.Timeout("always fails")):
        with pytest.raises(requests.exceptions.Timeout):
            client.post_info({"type": "meta"})


def test_http_4xx_not_retried():
    """A 4xx HTTPError must be raised immediately without retrying."""
    client = HyperliquidClient(network="testnet", max_retries=3)

    http_err_resp = MagicMock()
    http_err_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("404")

    with patch("src.hl.client.requests.post", return_value=http_err_resp) as mock_post:
        with pytest.raises(requests.exceptions.HTTPError):
            client.post_info({"type": "bad"})

    # Should only have been called once (no retries on 4xx)
    assert mock_post.call_count == 1


def test_retry_backoff_sleep_called():
    """Verify that time.sleep is called between retries with increasing delays."""
    client = HyperliquidClient(network="testnet", max_retries=3)
    success_resp = MagicMock()
    success_resp.json.return_value = {}
    success_resp.raise_for_status = MagicMock()

    with patch("src.hl.client.time.sleep") as mock_sleep, \
         patch("src.hl.client.requests.post", side_effect=[
             requests.exceptions.Timeout(),
             requests.exceptions.Timeout(),
             success_resp,
         ]):
        client.post_info({"type": "meta"})

    assert mock_sleep.call_count == 2
    # Back-off: 0.5s after attempt 0, 1.0s after attempt 1
    assert mock_sleep.call_args_list[0] == call(0.5)
    assert mock_sleep.call_args_list[1] == call(1.0)
