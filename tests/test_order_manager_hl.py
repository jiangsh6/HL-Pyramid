"""
HL Phase 4 — Order manager and config mainnet-rejection tests.

All 4 named tests:
  test_order_has_cloid_field
  test_mark_filled_hl_updates_all_fields
  test_mark_canceled_sets_status
  test_mainnet_mode_rejected_by_config
"""
from __future__ import annotations

from datetime import datetime
import os
from unittest.mock import patch

import pytest

from src.core.models import ActionType
from src.execution.order_manager import Order, build_order, mark_canceled, mark_filled_hl
from src.hl.order_placer import HLOrderResponse
from tests._helpers import base_config, make_state
from src.core.models import Decision


# ── Named tests ───────────────────────────────────────────────────────────────

def test_order_has_cloid_field():
    """Order dataclass has a cloid field that defaults to None."""
    order = Order(
        action=ActionType.BUY_ADDON,
        symbol="BTC",
        qty=0,
        order_type="limit",
        limit_price=50000.0,
        status="pending",
        created_at=datetime.now(),
    )
    assert hasattr(order, "cloid")
    assert order.cloid is None


def test_mark_filled_hl_updates_all_fields():
    """mark_filled_hl sets exchange_status, hl_oid, filled_qty, avg_fill_px."""
    order = Order(
        action=ActionType.BUY_ADDON,
        symbol="BTC",
        qty=0,
        order_type="limit",
        limit_price=50000.0,
        status="pending",
        created_at=datetime.now(),
        cloid="test-cloid-123",
    )
    response = HLOrderResponse(
        status="ok",
        cloid="test-cloid-123",
        hl_oid=98765,
        filled_sz=0.1,
        avg_fill_px=50150.0,
    )

    updated = mark_filled_hl(order, response)

    assert updated.exchange_status  == "filled"
    assert updated.hl_oid           == 98765
    assert updated.filled_qty == pytest.approx(0.1)
    assert updated.avg_fill_px      == pytest.approx(50150.0)
    assert updated is order   # mutates in-place


def test_mark_canceled_sets_status():
    """mark_canceled sets exchange_status to 'canceled'."""
    order = Order(
        action=ActionType.BUY_ADDON,
        symbol="BTC",
        qty=0,
        order_type="limit",
        limit_price=50000.0,
        status="pending",
        created_at=datetime.now(),
    )

    updated = mark_canceled(order)

    assert updated.exchange_status == "canceled"
    assert updated is order   # mutates in-place


def test_mainnet_mode_rejected_by_config():
    """bot.mode='mainnet' raises ValueError with a clear message."""
    from src.core.config_loader import validate_config
    config = base_config()
    config.bot["mode"] = "mainnet"

    with pytest.raises(ValueError, match="mainnet mode is not enabled"):
        validate_config(config)


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_order_has_hl_fields_with_correct_defaults():
    """All HL Phase 4 fields on Order default to safe values."""
    order = Order(
        action=ActionType.SELL_REDUCE_ADDON,
        symbol="BTC",
        qty=0,
        order_type="limit",
        limit_price=49000.0,
        status="pending",
        created_at=datetime.now(),
    )
    assert order.cloid            is None
    assert order.hl_oid           is None
    assert order.exchange_status  == "pending"
    assert order.filled_qty == pytest.approx(0.0)
    assert order.avg_fill_px      is None


def test_build_order_still_works_without_hl_fields():
    """build_order() remains backward-compatible; HL fields default to None."""
    decision = Decision(action=ActionType.BUY_BASE, qty=10, reason="test")
    order    = build_order(decision, symbol="BTC", order_type="limit", limit_price=100.0)

    assert order.action == ActionType.BUY_BASE
    assert order.cloid  is None
    assert order.hl_oid is None


def test_mark_filled_hl_handles_none_avg_fill_px():
    """mark_filled_hl with avg_fill_px=None stores None correctly."""
    order = Order(
        action=ActionType.BUY_ADDON,
        symbol="BTC",
        qty=0,
        order_type="limit",
        limit_price=50000.0,
        status="pending",
        created_at=datetime.now(),
    )
    response = HLOrderResponse(
        status="ok",
        hl_oid=111,
        filled_sz=0.05,
        avg_fill_px=None,
    )

    updated = mark_filled_hl(order, response)

    assert updated.avg_fill_px is None
    assert updated.filled_qty == pytest.approx(0.05)


def test_testnet_mode_still_accepted():
    """bot.mode='testnet' must still pass validation after the mainnet guard."""
    from src.core.config_loader import validate_config
    config = base_config()
    config.bot["mode"] = "testnet"
    # Must not raise — testnet is explicitly allowed
    # But we also need data.source to be compatible (paper config uses yfinance)
    # so just check the mode rule alone by loading the BTC config
    from src.core.config_loader import load_config
    with patch.dict(os.environ, {"HL_TESTNET_ACCOUNT_ADDRESS": "0x1111111111111111111111111111111111111111"}, clear=False):
        btc_cfg = load_config("config/btc_long_thesis.yaml")
    validate_config(btc_cfg)   # must not raise
