"""
HL Phase 4 — Order placer tests.

All 8 named tests:
  test_generate_cloid_returns_valid_uuid
  test_generate_cloid_is_unique_across_calls
  test_place_order_sends_correct_payload
  test_place_order_returns_ok_response_on_success
  test_place_order_returns_err_on_network_failure
  test_cancel_order_returns_true_on_success
  test_cancel_order_returns_false_when_not_found
  test_reduce_only_set_for_sell_orders
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.hl.client import TESTNET_URL
from src.hl.order_placer import (
    HLOrderRequest, HLOrderResponse,
    cancel_order, generate_cloid, place_order,
)

# Fake test private key — valid 32-byte Ethereum key, NOT a real key.
FAKE_KEY = "0x" + "a" * 64


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _mock_client(universe=None):
    """Build a MagicMock HyperliquidClient with a testnet base_url."""
    c = MagicMock()
    c.post_info.return_value = {
        "universe": universe or [{"name": "BTC"}, {"name": "ETH"}]
    }
    c.base_url = TESTNET_URL
    c._timeout = 10.0
    return c


def _ok_order_resp(oid: int = 42, filled: bool = False):
    if filled:
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [{"filled": {"totalSz": "0.1", "avgPx": "50100.0", "oid": oid}}]
                },
            },
        }
    return {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"resting": {"oid": oid}}]},
        },
    }


def _ok_cancel_resp():
    return {
        "status": "ok",
        "response": {
            "type": "cancel",
            "data": {"statuses": ["success"]},
        },
    }


def _mock_requests_post(json_body):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = json_body
    return r


# ── Named tests ───────────────────────────────────────────────────────────────

def test_generate_cloid_returns_valid_uuid():
    """generate_cloid() returns a string that parses as a valid UUID4."""
    cloid = generate_cloid()
    parsed = uuid.UUID(cloid, version=4)
    assert str(parsed) == cloid


def test_generate_cloid_is_unique_across_calls():
    """Each call to generate_cloid() returns a distinct value."""
    ids = {generate_cloid() for _ in range(20)}
    assert len(ids) == 20


def test_place_order_sends_correct_payload():
    """place_order sends a signed payload to the /exchange endpoint."""
    client    = _mock_client()
    mock_resp = _mock_requests_post(_ok_order_resp(42))

    order_req = HLOrderRequest(
        coin="BTC",
        is_buy=True,
        sz=0.1,
        limit_px=50000.0,
        order_type={"limit": {"tif": "Gtc"}},
    )

    with patch("src.hl.order_placer._requests.post", return_value=mock_resp) as mock_post:
        response = place_order(order_req, "0xABCD", FAKE_KEY, client)

    assert response.status == "ok"
    assert response.hl_oid  == 42

    # Verify POST target URL
    call_url = mock_post.call_args[0][0]
    assert call_url.endswith("/exchange")
    assert "testnet" in call_url

    # Verify payload structure
    payload = mock_post.call_args[1]["json"]
    assert payload["action"]["type"] == "order"
    assert "nonce"     in payload
    assert "signature" in payload
    assert set(payload["signature"].keys()) >= {"r", "s", "v"}

    # Verify the order wire contains the right coin and direction
    orders = payload["action"].get("orders", [])
    assert len(orders) == 1
    assert orders[0]["b"] is True   # is_buy


def test_place_order_returns_ok_response_on_success():
    """A successful /exchange response is parsed into HLOrderResponse(status='ok')."""
    client    = _mock_client()
    mock_resp = _mock_requests_post(_ok_order_resp(99, filled=True))

    order_req = HLOrderRequest(
        coin="BTC", is_buy=True, sz=0.1, limit_px=50000.0,
        order_type={"limit": {"tif": "Gtc"}},
    )

    with patch("src.hl.order_placer._requests.post", return_value=mock_resp):
        response = place_order(order_req, "0x1234", FAKE_KEY, client)

    assert response.status    == "ok"
    assert response.hl_oid    == 99
    assert response.filled_sz == pytest.approx(0.1)
    assert response.avg_fill_px == pytest.approx(50100.0)


def test_place_order_returns_err_on_network_failure():
    """A ConnectionError is caught and returned as HLOrderResponse(status='err')."""
    client = _mock_client()

    order_req = HLOrderRequest(
        coin="BTC", is_buy=True, sz=0.1, limit_px=50000.0,
        order_type={"limit": {"tif": "Gtc"}},
    )

    with patch("src.hl.order_placer._requests.post",
               side_effect=requests.exceptions.ConnectionError("refused")):
        response = place_order(order_req, "0x1234", FAKE_KEY, client)

    assert response.status == "err"
    assert response.error  == "network_error"


def test_cancel_order_returns_true_on_success():
    """cancel_order returns True when the exchange confirms cancellation."""
    client    = _mock_client()
    mock_resp = _mock_requests_post(_ok_cancel_resp())

    with patch("src.hl.order_placer._requests.post", return_value=mock_resp):
        result = cancel_order("BTC", 12345, "0xABCD", FAKE_KEY, client)

    assert result is True


def test_cancel_order_returns_false_when_not_found():
    """cancel_order returns False when the exchange reports an error."""
    client    = _mock_client()
    mock_resp = _mock_requests_post({"status": "err", "response": "Order not found"})

    with patch("src.hl.order_placer._requests.post", return_value=mock_resp):
        result = cancel_order("BTC", 99999, "0xABCD", FAKE_KEY, client)

    assert result is False


def test_reduce_only_set_for_sell_orders():
    """When reduce_only=True, the order wire must have 'r': True."""
    client    = _mock_client()
    mock_resp = _mock_requests_post(_ok_order_resp(7))

    order_req = HLOrderRequest(
        coin="BTC", is_buy=False, sz=0.05, limit_px=49000.0,
        order_type={"limit": {"tif": "Gtc"}},
        reduce_only=True,
    )

    with patch("src.hl.order_placer._requests.post", return_value=mock_resp) as mock_post:
        place_order(order_req, "0x1234", FAKE_KEY, client)

    payload = mock_post.call_args[1]["json"]
    orders  = payload["action"].get("orders", [])
    assert orders[0]["r"] is True   # reduce_only in wire format


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_place_order_coin_not_in_universe_returns_err():
    """place_order returns err when the coin is absent from the meta universe."""
    client = _mock_client(universe=[{"name": "ETH"}])  # no BTC

    order_req = HLOrderRequest(
        coin="BTC", is_buy=True, sz=0.1, limit_px=50000.0,
        order_type={"limit": {"tif": "Gtc"}},
    )

    response = place_order(order_req, "0x1234", FAKE_KEY, client)

    assert response.status == "err"


def test_place_order_exchange_error_response():
    """An 'err' status from /exchange is returned as HLOrderResponse(status='err')."""
    client    = _mock_client()
    mock_resp = _mock_requests_post({
        "status": "err",
        "response": "Insufficient margin",
    })

    order_req = HLOrderRequest(
        coin="BTC", is_buy=True, sz=0.1, limit_px=50000.0,
        order_type={"limit": {"tif": "Gtc"}},
    )

    with patch("src.hl.order_placer._requests.post", return_value=mock_resp):
        response = place_order(order_req, "0x1234", FAKE_KEY, client)

    assert response.status == "err"


def test_generate_cloid_converts_to_hl_hex_format():
    """A cloid from generate_cloid() can be converted to HL hex Cloid format."""
    from hyperliquid.utils.signing import Cloid
    cloid_str = generate_cloid()
    cloid_hex = "0x" + cloid_str.replace("-", "")
    hl_cloid  = Cloid.from_str(cloid_hex)
    assert hl_cloid.to_raw() == cloid_hex
