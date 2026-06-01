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
import os
from unittest.mock import MagicMock, patch

import pytest

from src.core.config_loader import load_config
from src.core.models import ActionType, BotState, Decision, LotRecord, OrderResultStatus, ThesisState
from src.execution.hl_broker import HL_TAKER_FEE, execute_hl
from src.hl.order_placer import HLOrderResponse
from tests._helpers import make_state

FAKE_KEY      = "0x" + "a" * 64
HL_CONFIG_PATH = "config/btc_long_thesis.yaml"
TESTNET_WALLET = "0x1111111111111111111111111111111111111111"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hl_config():
    with patch.dict(os.environ, {"HL_TESTNET_ACCOUNT_ADDRESS": TESTNET_WALLET}, clear=False):
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


def _make_decision(action=ActionType.BUY_ADDON, qty=0.1):
    return Decision(action=action, qty=qty, reason="test")


def _make_hl_state(qty=0.1):
    lot = LotRecord(lot_id="base", entry_price=50000.0, qty=qty,
                    entry_date=date(2026, 1, 1))
    return ThesisState(symbol="BTC", state=BotState.BASE_LONG,
                       base_lot=lot, current_position_qty=qty)


# ── Named tests ───────────────────────────────────────────────────────────────

def test_execute_hl_buy_maps_correctly():
    """BUY_ADDON maps to is_buy=True, reduce_only=False in the HLOrderRequest."""
    config   = _hl_config()
    state    = _make_hl_state()
    decision = _make_decision(ActionType.BUY_ADDON, qty=0.05)

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
    assert fill.status == OrderResultStatus.FILLED


def test_execute_hl_sell_sets_reduce_only():
    """SELL_REDUCE_ADDON maps to is_buy=False, reduce_only=True."""
    config   = _hl_config()
    state    = _make_hl_state()
    decision = _make_decision(ActionType.SELL_REDUCE_ADDON, qty=0.05)

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
    decision = _make_decision(ActionType.BUY_ADDON, qty=0.1)

    with patch("src.execution.hl_broker.place_order",
               return_value=_ok_response(filled_sz=0.1, avg_px=50100.0)):
        fill = execute_hl(decision, state, config, _mock_client(),
                          "0xABCD", FAKE_KEY, mark_price=50000.0)

    expected_commission = 0.1 * 50100.0 * HL_TAKER_FEE
    assert fill.commission == pytest.approx(expected_commission)


def test_execute_hl_raises_on_error_response():
    """HL exchange rejections normalize to OrderResult(REJECTED)."""
    config   = _hl_config()
    state    = _make_hl_state()
    decision = _make_decision(ActionType.BUY_ADDON, qty=0.1)

    err_response = HLOrderResponse(status="err", error="insufficient_margin")

    with patch("src.execution.hl_broker.place_order", return_value=err_response):
        result = execute_hl(decision, state, config, _mock_client(),
                            "0xABCD", FAKE_KEY, mark_price=50000.0)
    assert result.status == OrderResultStatus.REJECTED
    assert result.exchange_error_sanitized == "insufficient_margin"


def test_execute_hl_fill_price_from_response():
    """fill.fill_price comes from response.avg_fill_px when present."""
    config   = _hl_config()
    state    = _make_hl_state()
    decision = _make_decision(ActionType.BUY_ADDON, qty=0.1)

    with patch("src.execution.hl_broker.place_order",
               return_value=_ok_response(filled_sz=0.1, avg_px=50200.0)):
        fill = execute_hl(decision, state, config, _mock_client(),
                          "0xABCD", FAKE_KEY, mark_price=50000.0)

    assert fill.avg_fill_px == pytest.approx(50200.0)


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


def test_execute_hl_probe_aggressive_starter_pricing_is_probe_only():
    config = _hl_config()
    state = ThesisState(symbol="BTC")
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.005,
        reason="probe",
        new_state=BotState.STARTER_LONG,
    )

    passive = {}
    aggressive = {}

    def fake_passive(req, wallet, key, client):
        passive["limit_px"] = req.limit_px
        passive["tif"] = req.order_type["limit"]["tif"]
        return _ok_response(filled_sz=0.0, avg_px=None)

    def fake_aggressive(req, wallet, key, client):
        aggressive["limit_px"] = req.limit_px
        aggressive["tif"] = req.order_type["limit"]["tif"]
        return _ok_response(filled_sz=0.0, avg_px=None)

    with patch("src.execution.hl_broker.place_order", side_effect=fake_passive):
        execute_hl(decision, state, config, _mock_client(), "0x", FAKE_KEY, mark_price=72000.0)

    probe_execution = dict(config.execution)
    probe_execution.update(
        {
            "probe_starter_pricing_mode": "aggressive",
            "probe_starter_aggressiveness_bps": 5,
            "probe_max_oracle_deviation_bps": 20,
            "time_in_force": "gtc",
        }
    )
    probe_config = config.model_copy(update={"execution": probe_execution})

    with patch("src.execution.hl_broker.place_order", side_effect=fake_aggressive):
        execute_hl(decision, state, probe_config, _mock_client(), "0x", FAKE_KEY, mark_price=72000.0)

    assert passive["limit_px"] == pytest.approx(72072.0)
    assert aggressive["limit_px"] > 72000.0
    assert aggressive["limit_px"] <= 72000.0 * (1 + 20 / 10_000) + 1e-9
    assert passive["tif"] == "Gtc"
    assert aggressive["tif"] == "Gtc"


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_execute_hl_unsupported_action_raises():
    """HALT action is unsupported and normalizes to execution_error."""
    config   = _hl_config()
    state    = _make_hl_state()
    decision = Decision(action=ActionType.HALT, qty=0.0, reason="test")

    result = execute_hl(decision, state, config, _mock_client(),
                        "0xABCD", FAKE_KEY, mark_price=50000.0)
    assert result.status == OrderResultStatus.EXECUTION_ERROR


def test_execute_hl_exit_all_is_reduce_only():
    """EXIT_ALL is treated as a sell with reduce_only=True."""
    config   = _hl_config()
    state    = _make_hl_state(0.1)
    decision = _make_decision(ActionType.EXIT_ALL, qty=0.1)

    captured = {}

    def fake_place(req, wallet, key, client):
        captured["req"] = req
        return _ok_response(filled_sz=0.1, avg_px=49900.0)

    with patch("src.execution.hl_broker.place_order", side_effect=fake_place):
        execute_hl(decision, state, config, _mock_client(),
                   "0xABCD", FAKE_KEY, mark_price=50000.0)

    assert captured["req"].is_buy      is False
    assert captured["req"].reduce_only is True


def test_execute_hl_exit_all_uses_oracle_safe_exit_price():
    config = _hl_config()
    config.execution["exit_price_aggressiveness_bps"] = 2
    config.execution["max_oracle_deviation_bps"] = 5
    state = _make_hl_state(0.00125)
    decision = _make_decision(ActionType.EXIT_ALL, qty=0.00125)

    captured = {}

    def fake_place(req, wallet, key, client):
        captured["req"] = req
        return _ok_response(filled_sz=0.00125, avg_px=req.limit_px)

    with patch("src.execution.hl_broker.place_order", side_effect=fake_place):
        execute_hl(
            decision,
            state,
            config,
            _mock_client(),
            "0xABCD",
            FAKE_KEY,
            mark_price=71738.0,
        )

    assert captured["req"].reduce_only is True
    assert captured["req"].limit_px == pytest.approx(71724.0)
    assert captured["req"].limit_px > 71667.0


def test_oracle_rejected_exit_does_not_mutate_state_in_broker():
    config = _hl_config()
    state = _make_hl_state(0.00125)
    state_before_qty = state.current_position_qty
    decision = _make_decision(ActionType.EXIT_ALL, qty=0.00125)

    with patch("src.execution.hl_broker.place_order", return_value=HLOrderResponse(
        status="err",
        submitted_sz=0.00125,
        submitted_limit_px=71724.0,
        error="oracleRejected",
    )):
        result = execute_hl(
            decision,
            state,
            config,
            _mock_client(),
            "0xABCD",
            FAKE_KEY,
            mark_price=71738.0,
        )

    assert result.status == OrderResultStatus.REJECTED
    assert state.current_position_qty == pytest.approx(state_before_qty)
    assert state.base_lot is not None
    assert state.base_lot.qty == pytest.approx(0.00125)


def test_execute_hl_resting_order_does_not_fake_a_fill():
    """A resting order with filled_sz=0 must not be treated as an immediate fill."""
    config = _hl_config()
    state = _make_hl_state()
    decision = _make_decision(ActionType.BUY_STARTER, qty=0.001)

    with patch("src.execution.hl_broker.place_order", return_value=HLOrderResponse(
        status="ok",
        hl_oid=123,
        filled_sz=0.0,
        avg_fill_px=None,
    )):
        fill = execute_hl(decision, state, config, _mock_client(),
                          "0xABCD", FAKE_KEY, mark_price=50000.0)

    assert fill.filled_qty == pytest.approx(0.0)
    assert fill.realized_pnl == pytest.approx(0.0)
    assert fill.commission == pytest.approx(0.0)
    assert fill.status == OrderResultStatus.SUBMITTED_UNFILLED


def test_execute_hl_uses_formatted_submitted_qty_for_full_fill_classification():
    config = _hl_config()
    state = ThesisState(symbol="BTC")
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.0012443141755036015,
        reason="test",
        new_state=BotState.STARTER_LONG,
    )

    with patch("src.execution.hl_broker.place_order", return_value=HLOrderResponse(
        status="ok",
        hl_oid=321,
        submitted_sz=0.00124,
        submitted_limit_px=72401.0,
        filled_sz=0.00124,
        avg_fill_px=72325.0,
    )):
        result = execute_hl(
            decision, state, config, _mock_client(),
            "0xABCD", FAKE_KEY, mark_price=72329.0,
        )

    assert result.submitted_qty == pytest.approx(0.00124)
    assert result.filled_qty == pytest.approx(0.00124)
    assert result.remaining_qty == pytest.approx(0.0)
    assert result.status == OrderResultStatus.FILLED


def test_execute_hl_uses_formatted_submitted_qty_for_partial_fill_classification():
    config = _hl_config()
    state = ThesisState(symbol="BTC")
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.0012443141755036015,
        reason="test",
        new_state=BotState.STARTER_LONG,
    )

    with patch("src.execution.hl_broker.place_order", return_value=HLOrderResponse(
        status="ok",
        hl_oid=322,
        submitted_sz=0.00124,
        submitted_limit_px=72401.0,
        filled_sz=0.00062,
        avg_fill_px=72325.0,
    )):
        result = execute_hl(
            decision, state, config, _mock_client(),
            "0xABCD", FAKE_KEY, mark_price=72329.0,
        )

    assert result.submitted_qty == pytest.approx(0.00124)
    assert result.filled_qty == pytest.approx(0.00062)
    assert result.remaining_qty == pytest.approx(0.00062)
    assert result.status == OrderResultStatus.PARTIALLY_FILLED
