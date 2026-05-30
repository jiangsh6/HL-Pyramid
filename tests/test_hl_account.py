"""
HL Phase 3 — Account snapshot tests.

All 5 named tests:
  test_get_account_snapshot_parses_response_correctly
  test_get_account_snapshot_empty_positions_returns_valid_snapshot
  test_get_account_snapshot_raises_on_bad_response
  test_hl_position_parses_liquidation_price
  test_hl_position_handles_null_liquidation_price
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.hl.account import HLAccountSnapshot, HLPosition, HLAPIError, get_account_snapshot


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _mock_client(response):
    client = MagicMock()
    client.post_info.return_value = response
    return client


_VALID_RESPONSE = {
    "marginSummary": {
        "accountValue": "10000.0",
        "totalMarginUsed": "1500.0",
        "withdrawable": "8500.0",
    },
    "assetPositions": [
        {
            "position": {
                "coin": "BTC",
                "szi": "0.1",
                "entryPx": "50000.0",
                "positionValue": "5100.0",
                "unrealizedPnl": "100.0",
                "liquidationPx": "45000.0",
                "marginUsed": "1500.0",
                "leverage": {"value": 3.0, "type": "cross"},
                "markPx": "51000.0",
            }
        }
    ],
}


# ── Named tests ───────────────────────────────────────────────────────────────

def test_get_account_snapshot_parses_response_correctly():
    """Parsed snapshot fields match the mocked API response."""
    client   = _mock_client(_VALID_RESPONSE)
    snapshot = get_account_snapshot("0x1234", client)

    assert snapshot.account_value == 10000.0
    assert snapshot.margin_used   == 1500.0
    assert snapshot.withdrawable  == 8500.0
    assert len(snapshot.positions) == 1

    pos = snapshot.positions[0]
    assert pos.coin          == "BTC"
    assert pos.contracts     == 0.1
    assert pos.entry_price   == 50000.0
    assert pos.mark_price    == 51000.0
    assert pos.unrealized_pnl == 100.0
    assert pos.liquidation_price == 45000.0
    assert pos.margin_used   == 1500.0
    assert pos.leverage      == 3.0


def test_get_account_snapshot_empty_positions_returns_valid_snapshot():
    """Wallet with no open positions returns a snapshot with empty positions list."""
    response = {
        "marginSummary": {
            "accountValue": "5000.0",
            "totalMarginUsed": "0.0",
            "withdrawable": "5000.0",
        },
        "assetPositions": [],
    }
    client   = _mock_client(response)
    snapshot = get_account_snapshot("0xABCD", client)

    assert snapshot.account_value == 5000.0
    assert snapshot.positions == []


def test_get_account_snapshot_raises_on_bad_response():
    """A non-dict API response raises HLAPIError."""
    client = _mock_client("invalid_string_response")
    with pytest.raises(HLAPIError):
        get_account_snapshot("0x1234", client)


def test_hl_position_parses_liquidation_price():
    """liquidation_price is parsed as a float when liquidationPx is present."""
    client   = _mock_client(_VALID_RESPONSE)
    snapshot = get_account_snapshot("0x1234", client)

    assert snapshot.positions[0].liquidation_price == 45000.0
    assert isinstance(snapshot.positions[0].liquidation_price, float)


def test_hl_position_handles_null_liquidation_price():
    """liquidation_price is None when liquidationPx is null in the response."""
    response = {
        "marginSummary": {
            "accountValue": "5000.0",
            "totalMarginUsed": "500.0",
            "withdrawable": "4500.0",
        },
        "assetPositions": [
            {
                "position": {
                    "coin": "ETH",
                    "szi": "1.0",
                    "entryPx": "3000.0",
                    "positionValue": "3000.0",
                    "unrealizedPnl": "0.0",
                    "liquidationPx": None,
                    "marginUsed": "500.0",
                    "leverage": 5.0,
                    "markPx": "3000.0",
                }
            }
        ],
    }
    client   = _mock_client(response)
    snapshot = get_account_snapshot("0x5678", client)

    assert snapshot.positions[0].liquidation_price is None


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_get_account_snapshot_raises_when_margin_summary_missing():
    """Missing marginSummary dict raises HLAPIError."""
    client = _mock_client({"assetPositions": []})
    with pytest.raises(HLAPIError, match="marginSummary"):
        get_account_snapshot("0x1234", client)


def test_get_account_snapshot_mark_price_derived_from_position_value():
    """When markPx is absent, mark_price is derived from positionValue / |szi|."""
    response = {
        "marginSummary": {
            "accountValue": "10000.0",
            "totalMarginUsed": "1000.0",
            "withdrawable": "9000.0",
        },
        "assetPositions": [
            {
                "position": {
                    "coin": "SOL",
                    "szi": "2.0",
                    "entryPx": "200.0",
                    "positionValue": "420.0",  # 2 * 210
                    "unrealizedPnl": "20.0",
                    "liquidationPx": "100.0",
                    "marginUsed": "1000.0",
                    "leverage": 2.0,
                    # markPx intentionally absent
                }
            }
        ],
    }
    client   = _mock_client(response)
    snapshot = get_account_snapshot("0x9999", client)

    assert snapshot.positions[0].mark_price == pytest.approx(210.0)


def test_client_post_info_called_with_correct_payload():
    """Verifies the correct payload is sent to the HL API."""
    client   = _mock_client(_VALID_RESPONSE)
    get_account_snapshot("0xDEAD", client)

    client.post_info.assert_called_once_with(
        {"type": "clearinghouseState", "user": "0xDEAD"}
    )
