"""
HL Phase 4 — HL broker tests.

All 6 named tests:
  test_execute_hl_buy_maps_correctly
  test_execute_hl_sell_sets_reduce_only
  test_execute_hl_commission_is_taker_fee
  test_execute_hl_raises_on_error_response
  test_execute_hl_fill_price_from_response
  test_execute_hl_limit_px_offset_applied_correctly
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.core.config_loader import load_config
from src.core.models import ActionType, BotState, Decision, LotRecord, ThesisState
from src.execution.hl_broker import HLExecutionError, HL_TAKER_FEE, execute_hl
from src.hl.order_placer import HLOrderResponse
from tests._helpers import make_state

FAKE_KEY      = "0x" + "a" * 64
HL_CONFIG_PATH = "config/btc_long_thesis.yaml"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hl_config():
    return load_config(HL_CONFIG_PATH)


def _mock_client():
    c = MagicMock()
    c.post_info.return_value = {"universe": [{"name": "BTC"}]}
    c.base_url   = "https://api.hyperliquid-testnet.xyz"
    c._timeout   = 10.0
    return c


def _ok_response(oid=1, filled_sz=0.1, avg_px=50050.0):
    return HLOrderResponse(
        status="ok",
        hl_oid=oid,
        filled_sz=filled_sz,
        avg_fill_px=avg_px,
    )


def _make_decision(action=ActionType.BUY_ADDON, contracts=0.1):
    return Decision(action=action, contracts=contracts, reason="test")


def _make_hl_state(contracts=0.1):
    lot = LotRecord(lot_id="base", entry_price=50000.0, contracts=contracts,
                    entry_date=date(2026, 1, 1))
    return ThesisState(symbol="BTC", state=BotState.BASE_LONG,
                       base_lot=lot, current_position_contracts=contracts)


# ── Named tests ───────────────────────────────────────────────────────────────

def test_execute_hl_buy_maps_correctly():
    """BUY_ADDON maps to is_buy=True, reduce_only=False in the HLOrderRequest."""
    config   = _hl_config()
    state    = _make_hl_state()
    decision = _make_decision(ActionType.BUY_ADDON, contracts=0.05)

    captured_request = {}

    def fake_place_order(req, wallet, key, client):
        captured_request["req"] = req
        return _ok_response(filled_sz=0.05, avg_px=50050.0)

    with patch("src.execution.hl_broker.place_order", side_effect=fake_place_order):
        fill = execute_hl(decision, state, config, _mock_client(),
                          "0xABCD", FAKE_KEY, mark_price=50000.0)

    req = captured_request["req"]
    assert req.is_buy      is True
    assert req.reduce_only is False
    assert req.coin        == "BTC"
    assert fill.action     == ActionType.BUY_ADDON


def test_execute_hl_sell_sets_reduce_only():
    """SELL_REDUCE_ADDON maps to is_buy=False, reduce_only=True."""
    config   = _hl_config()
    state    = _make_hl_state()
    decision = _make_decision(ActionType.SELL_REDUCE_ADDON, contracts=0.05)

    captured_request = {}

    def fake_place_order(req, wallet, key, client):
        captured_request["req"] = req
        return _ok_response(filled_sz=0.05, avg_px=49950.0)

    with patch("src.execution.hl_broker.place_order", side_effect=fake_place_order):
        execute_hl(decision, state, config, _mock_client(),
                   "0xABCD", FAKE_KEY, mark_price=50000.0)

    req = captured_request["req"]
    assert req.is_buy      is False
    assert req.reduce_only is True


def test_execute_hl_commission_is_taker_fee():
    """Fill.commission = filled_sz * fill_price * HL_TAKER_FEE (0.05%)."""
    config   = _hl_config()
    state    = _make_hl_state()
    decision = _make_decision(ActionType.BUY_ADDON, contracts=0.1)

    with patch("src.execution.hl_broker.place_order",
               return_value=_ok_response(filled_sz=0.1, avg_px=50100.0)):
        fill = execute_hl(decision, state, config, _mock_client(),
                          "0xABCD", FAKE_KEY, mark_price=50000.0)

    expected_commission = 0.1 * 50100.0 * HL_TAKER_FEE
    assert fill.commission == pytest.approx(expected_commission)


def test_execute_hl_raises_on_error_response():
    """HLExecutionError is raised when place_order returns status='err'."""
    config   = _hl_config()
    state    = _make_hl_state()
    decision = _make_decision(ActionType.BUY_ADDON, contracts=0.1)

    err_response = HLOrderResponse(status="err", error="insufficient_margin")

    with patch("src.execution.hl_broker.place_order", return_value=err_response):
        with pytest.raises(HLExecutionError, match="insufficient_margin"):
            execute_hl(decision, state, config, _mock_client(),
                       "0xABCD", FAKE_KEY, mark_price=50000.0)


def test_execute_hl_fill_price_from_response():
    """fill.fill_price comes from response.avg_fill_px when present."""
    config   = _hl_config()
    state    = _make_hl_state()
    decision = _make_decision(ActionType.BUY_ADDON, contracts=0.1)

    with patch("src.execution.hl_broker.place_order",
               return_value=_ok_response(filled_sz=0.1, avg_px=50200.0)):
        fill = execute_hl(decision, state, config, _mock_client(),
                          "0xABCD", FAKE_KEY, mark_price=50000.0)

    assert fill.fill_price == pytest.approx(50200.0)


def test_execute_hl_limit_px_offset_applied_correctly():
    """limit_px = mark * (1 + offset_bps/10000) for buys, (1 - ...) for sells."""
    config     = _hl_config()
    state      = _make_hl_state()
    mark_price = 50000.0
    offset_bps = config.execution["limit_offset_bps"]   # 10 bps
    offset     = offset_bps / 10_000.0

    captured_buy  = {}
    captured_sell = {}

    def fake_buy(req, wallet, key, client):
        captured_buy["limit_px"] = req.limit_px
        return _ok_response(avg_px=req.limit_px)

    def fake_sell(req, wallet, key, client):
        captured_sell["limit_px"] = req.limit_px
        return _ok_response(avg_px=req.limit_px)

    with patch("src.execution.hl_broker.place_order", side_effect=fake_buy):
        execute_hl(_make_decision(ActionType.BUY_ADDON, 0.1),
                   state, config, _mock_client(), "0x", FAKE_KEY, mark_price)

    with patch("src.execution.hl_broker.place_order", side_effect=fake_sell):
        execute_hl(_make_decision(ActionType.SELL_REDUCE_ADDON, 0.1),
                   state, config, _mock_client(), "0x", FAKE_KEY, mark_price)

    assert captured_buy["limit_px"]  == pytest.approx(mark_price * (1 + offset))
    assert captured_sell["limit_px"] == pytest.approx(mark_price * (1 - offset))


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_execute_hl_unsupported_action_raises():
    """HALT action is not a buy or sell and raises HLExecutionError."""
    config   = _hl_config()
    state    = _make_hl_state()
    decision = Decision(action=ActionType.HALT, contracts=0.0, reason="test")

    with pytest.raises(HLExecutionError, match="Unsupported action"):
        execute_hl(decision, state, config, _mock_client(),
                   "0xABCD", FAKE_KEY, mark_price=50000.0)


def test_execute_hl_exit_all_is_reduce_only():
    """EXIT_ALL is treated as a sell with reduce_only=True."""
    config   = _hl_config()
    state    = _make_hl_state(0.1)
    decision = _make_decision(ActionType.EXIT_ALL, contracts=0.1)

    captured = {}

    def fake_place(req, wallet, key, client):
        captured["req"] = req
        return _ok_response(filled_sz=0.1, avg_px=49900.0)

    with patch("src.execution.hl_broker.place_order", side_effect=fake_place):
        execute_hl(decision, state, config, _mock_client(),
                   "0xABCD", FAKE_KEY, mark_price=50000.0)

    assert captured["req"].is_buy      is False
    assert captured["req"].reduce_only is True
