"""
HL Phase 4 — Auth module tests.

All 4 named tests:
  test_get_private_key_returns_value_from_env
  test_get_private_key_raises_when_env_not_set
  test_private_key_never_appears_in_error_message
  test_build_signer_returns_account_object
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from src.hl.auth import build_signer, get_private_key

# Use a deterministic fake key that is a valid 32-byte Ethereum private key.
# It is NOT a real key — never use "a" * 64 in production.
FAKE_KEY = "0x" + "a" * 64


# ── Named tests ───────────────────────────────────────────────────────────────

def test_get_private_key_returns_value_from_env():
    """get_private_key() returns the env var value when it is set."""
    with patch.dict(os.environ, {"HL_PRIVATE_KEY": FAKE_KEY}):
        key = get_private_key()
    assert key == FAKE_KEY


def test_get_private_key_raises_when_env_not_set():
    """get_private_key() raises ValueError when HL_PRIVATE_KEY is not set."""
    env = {k: v for k, v in os.environ.items() if k != "HL_PRIVATE_KEY"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="HL_PRIVATE_KEY env var not set"):
            get_private_key()


def test_private_key_never_appears_in_error_message():
    """
    Error messages must never expose the private key value.

    We set a recognisable fake key, then trigger a network failure during
    order placement and confirm the key string is absent from the error.
    """
    import requests
    from unittest.mock import MagicMock
    from src.hl.order_placer import HLOrderRequest, place_order

    distinctive_key = "0x" + "c" * 64   # recognisable sentinel

    mock_client = MagicMock()
    mock_client.post_info.return_value = {"universe": [{"name": "BTC"}]}
    mock_client.base_url = "https://api.hyperliquid-testnet.xyz"
    mock_client._timeout = 10.0

    order_req = HLOrderRequest(
        coin="BTC",
        is_buy=True,
        sz=0.1,
        limit_px=50000.0,
        order_type={"limit": {"tif": "Gtc"}},
    )

    with patch("src.hl.order_placer._requests.post",
               side_effect=requests.exceptions.ConnectionError("refused")):
        response = place_order(order_req, "0x1234", distinctive_key, mock_client)

    assert response.status == "err"
    # The key value must NOT appear in any part of the error message
    assert distinctive_key not in (response.error or "")
    assert "c" * 64 not in (response.error or "")


def test_build_signer_returns_account_object():
    """build_signer() returns an eth_account LocalAccount with a valid address."""
    from eth_account.signers.local import LocalAccount
    wallet = build_signer(FAKE_KEY)

    assert isinstance(wallet, LocalAccount)
    assert wallet.address.startswith("0x")
    assert len(wallet.address) == 42   # 0x + 40 hex chars


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_get_private_key_raises_for_empty_string():
    """An empty HL_PRIVATE_KEY is treated as unset and raises ValueError."""
    with patch.dict(os.environ, {"HL_PRIVATE_KEY": ""}):
        with pytest.raises(ValueError, match="HL_PRIVATE_KEY"):
            get_private_key()


def test_build_signer_produces_deterministic_address():
    """Same key always produces the same address (deterministic)."""
    addr1 = build_signer(FAKE_KEY).address
    addr2 = build_signer(FAKE_KEY).address
    assert addr1 == addr2
