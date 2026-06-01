from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from unittest.mock import MagicMock

from scripts.probe_hl_starter_order import (
    apply_starter_order_result,
    get_live_probe_prices,
    resolve_probe_starter_pricing,
)
from src.core.config_loader import load_config
from src.core.models import (
    ActionType,
    BotState,
    Decision,
    OrderResult,
    OrderResultStatus,
    ThesisState,
)


@pytest.fixture(autouse=True)
def _stub_hl_env(monkeypatch):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0x1111111111111111111111111111111111111111")
    monkeypatch.setenv("HL_TESTNET_AGENT_PRIVATE_KEY", "0x" + "1" * 64)


def test_aggressive_10_bps_buy_price_above_mark_and_within_cap():
    cfg = load_config("config/btc_testnet_realistic_probe.yaml")
    result = resolve_probe_starter_pricing(
        config=cfg,
        mark_price=72000.0,
        sz_decimals=5,
        pricing_mode="aggressive",
        aggressiveness_bps=10.0,
        max_oracle_deviation_bps=20.0,
        max_aggressiveness_bps=20.0,
        time_in_force="gtc",
    )

    assert result["blocker"] is None
    assert result["formatted_probe_px"] > 72000.0
    assert result["formatted_probe_px"] <= 72000.0 * (1 + 20 / 10_000)


def test_live_price_helper_extracts_mark_and_oracle():
    client = MagicMock()
    client.post_info.return_value = [
        {"universe": [{"name": "ETH"}, {"name": "BTC"}]},
        [{"markPx": "3000", "oraclePx": "3001"}, {"markPx": "73123.4", "oraclePx": "73120.1"}],
    ]

    mark_px, oracle_px = get_live_probe_prices(client, "BTC")

    assert mark_px == pytest.approx(73123.4)
    assert oracle_px == pytest.approx(73120.1)
    client.post_info.assert_called_once_with({"type": "metaAndAssetCtxs"})


def test_live_price_helper_falls_back_to_oracle_when_mark_missing():
    client = MagicMock()
    client.post_info.return_value = [
        {"universe": [{"name": "BTC"}]},
        [{"oraclePx": "73120.1"}],
    ]

    mark_px, oracle_px = get_live_probe_prices(client, "BTC")

    assert mark_px is None
    assert oracle_px == pytest.approx(73120.1)


def test_live_price_helper_returns_none_when_mark_and_oracle_missing():
    client = MagicMock()
    client.post_info.return_value = [
        {"universe": [{"name": "BTC"}]},
        [{"funding": "0.0"}],
    ]

    mark_px, oracle_px = get_live_probe_prices(client, "BTC")

    assert mark_px is None
    assert oracle_px is None


def test_aggressive_starter_price_uses_live_price_not_hardcoded_72000():
    cfg = load_config("config/btc_testnet_realistic_probe.yaml")
    result = resolve_probe_starter_pricing(
        config=cfg,
        mark_price=73123.4,
        sz_decimals=5,
        pricing_mode="aggressive",
        aggressiveness_bps=10.0,
        max_oracle_deviation_bps=20.0,
        max_aggressiveness_bps=20.0,
        time_in_force="ioc",
        oracle_price=73120.1,
    )

    assert result["blocker"] is None
    assert result["raw_probe_px"] > 73123.4
    assert result["formatted_probe_px"] > 72000.0


def test_aggressive_15_bps_buy_price_above_mark_and_within_cap():
    cfg = load_config("config/btc_testnet_realistic_probe.yaml")
    result = resolve_probe_starter_pricing(
        config=cfg,
        mark_price=72000.0,
        sz_decimals=5,
        pricing_mode="aggressive",
        aggressiveness_bps=15.0,
        max_oracle_deviation_bps=20.0,
        max_aggressiveness_bps=20.0,
        time_in_force="gtc",
    )

    assert result["blocker"] is None
    assert result["formatted_probe_px"] > 72000.0
    assert result["formatted_probe_px"] <= 72000.0 * (1 + 20 / 10_000)


def test_cross_mode_refuses_when_requested_bps_exceeds_oracle_cap():
    cfg = load_config("config/btc_testnet_realistic_probe.yaml")
    result = resolve_probe_starter_pricing(
        config=cfg,
        mark_price=72000.0,
        sz_decimals=5,
        pricing_mode="cross",
        aggressiveness_bps=25.0,
        max_oracle_deviation_bps=20.0,
        max_aggressiveness_bps=25.0,
        time_in_force="gtc",
    )

    assert result["blocker"] == "starter_cross_exceeds_max_oracle_deviation"


def test_starter_order_result_submitted_unfilled_keeps_flat_with_pending():
    state = ThesisState(symbol="BTC", state=BotState.FLAT)
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.005,
        reason="probe",
        new_state=BotState.STARTER_LONG,
    )
    result = OrderResult(
        status=OrderResultStatus.SUBMITTED_UNFILLED,
        oid=1,
        action=ActionType.BUY_STARTER,
        side="buy",
        reduce_only=False,
        submitted_qty=0.005,
        filled_qty=0.0,
        remaining_qty=0.005,
        limit_px=72072.0,
        raw_status_sanitized="resting",
        state_before=BotState.FLAT.value,
        intended_state_after=BotState.STARTER_LONG.value,
        applied_state_after=BotState.FLAT.value,
        created_at=datetime.now(timezone.utc),
    )

    from scripts.probe_hl_starter_order import _pending_from_order_result

    state.pending_order = _pending_from_order_result(state, decision, result)
    assert state.state == BotState.FLAT
    assert state.pending_order is not None
    assert state.pending_order.qty_remaining == pytest.approx(0.005)


def test_ioc_unfilled_stays_flat_without_pending_order():
    state = ThesisState(symbol="BTC", state=BotState.FLAT)
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.005,
        reason="probe",
        new_state=BotState.STARTER_LONG,
    )
    result = OrderResult(
        status=OrderResultStatus.SUBMITTED_UNFILLED,
        oid=None,
        action=ActionType.BUY_STARTER,
        side="buy",
        reduce_only=False,
        submitted_qty=0.005,
        filled_qty=0.0,
        remaining_qty=0.005,
        limit_px=73197.0,
        raw_status_sanitized="ioc_unfilled",
        state_before=BotState.FLAT.value,
        intended_state_after=BotState.STARTER_LONG.value,
        applied_state_after=BotState.FLAT.value,
        created_at=datetime.now(timezone.utc),
    )

    fill, risk_status = apply_starter_order_result(state, decision, result, is_ioc=True)

    assert fill is None
    assert risk_status == "blocked"
    assert state.state == BotState.FLAT
    assert state.current_position_qty == 0.0
    assert state.pending_order is None


def test_ioc_no_match_rejection_stays_flat_without_pending_order():
    state = ThesisState(symbol="BTC", state=BotState.FLAT)
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.005,
        reason="probe",
        new_state=BotState.STARTER_LONG,
    )
    result = OrderResult(
        status=OrderResultStatus.REJECTED,
        oid=None,
        action=ActionType.BUY_STARTER,
        side="buy",
        reduce_only=False,
        submitted_qty=0.005,
        filled_qty=0.0,
        remaining_qty=0.005,
        limit_px=73197.0,
        exchange_error_sanitized="Order could not immediately match against any resting orders. asset=3",
        raw_status_sanitized="error",
        state_before=BotState.FLAT.value,
        intended_state_after=BotState.STARTER_LONG.value,
        applied_state_after=BotState.FLAT.value,
        created_at=datetime.now(timezone.utc),
    )

    fill, risk_status = apply_starter_order_result(state, decision, result, is_ioc=True)

    assert fill is None
    assert risk_status == "blocked"
    assert state.state == BotState.FLAT
    assert state.halted is False
    assert state.current_position_qty == 0.0
    assert state.pending_order is None


def test_ioc_full_fill_transitions_to_starter_long():
    state = ThesisState(symbol="BTC", state=BotState.FLAT)
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.005,
        reason="probe",
        new_state=BotState.STARTER_LONG,
    )
    result = OrderResult(
        status=OrderResultStatus.FILLED,
        oid=1,
        action=ActionType.BUY_STARTER,
        side="buy",
        reduce_only=False,
        submitted_qty=0.005,
        filled_qty=0.005,
        remaining_qty=0.0,
        avg_fill_px=73190.0,
        limit_px=73197.0,
        raw_status_sanitized="filled",
        state_before=BotState.FLAT.value,
        intended_state_after=BotState.STARTER_LONG.value,
        applied_state_after=BotState.STARTER_LONG.value,
        created_at=datetime.now(timezone.utc),
    )

    fill, risk_status = apply_starter_order_result(state, decision, result, is_ioc=True)

    assert fill is not None
    assert risk_status == "ok"
    assert state.state == BotState.STARTER_LONG
    assert state.current_position_qty == pytest.approx(0.005)
    assert state.original_base_qty == pytest.approx(0.005)
    assert state.pending_order is None


def test_ioc_partial_fill_applies_only_filled_qty_without_pending_order():
    state = ThesisState(symbol="BTC", state=BotState.FLAT)
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.005,
        reason="probe",
        new_state=BotState.STARTER_LONG,
    )
    result = OrderResult(
        status=OrderResultStatus.PARTIALLY_FILLED,
        oid=1,
        action=ActionType.BUY_STARTER,
        side="buy",
        reduce_only=False,
        submitted_qty=0.005,
        filled_qty=0.002,
        remaining_qty=0.003,
        avg_fill_px=73190.0,
        limit_px=73197.0,
        raw_status_sanitized="filled",
        state_before=BotState.FLAT.value,
        intended_state_after=BotState.STARTER_LONG.value,
        applied_state_after=BotState.STARTER_LONG.value,
        created_at=datetime.now(timezone.utc),
    )

    fill, risk_status = apply_starter_order_result(state, decision, result, is_ioc=True)

    assert fill is not None
    assert risk_status == "ok"
    assert state.state == BotState.STARTER_LONG
    assert state.current_position_qty == pytest.approx(0.002)
    assert state.original_base_qty == pytest.approx(0.002)
    assert state.pending_order is None


def test_starter_order_result_fill_transitions_to_starter_long():
    from src.core.models import Fill
    from src.reporting.state_writer import apply_fill

    state = ThesisState(symbol="BTC", state=BotState.FLAT)
    fill = Fill(
        action=ActionType.BUY_STARTER,
        qty=0.005,
        fill_price=72036.0,
        slippage_bps=0.0,
        realized_pnl=0.0,
        commission=0.0,
        timestamp=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )
    apply_fill(state, fill)
    state.state = BotState.STARTER_LONG

    assert state.state == BotState.STARTER_LONG
    assert state.original_base_qty == pytest.approx(0.005)
    assert state.base_lot is not None
    assert state.base_lot.qty == pytest.approx(0.005)
