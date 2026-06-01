"""
HL Phase 3 — Risk manager tests for liquidation proximity and margin ratio.

All 4 named tests:
  test_blocks_when_approaching_liquidation_price
  test_blocks_when_margin_ratio_too_high
  test_allows_when_liq_price_far_away
  test_liquidation_warn_in_validators_when_liq_above_stop
"""
from __future__ import annotations

import pytest

from src.core.models import ActionType, BotState, Decision
from src.risk.risk_manager import check_order_allowed
from src.risk.validators import validate_data
from tests._helpers import base_config, make_indicators, make_state


# ── Named tests ───────────────────────────────────────────────────────────────

def test_blocks_when_approaching_liquidation_price():
    """Order is blocked when liquidation price is within 5% of current price."""
    state = make_state(BotState.BASE_LONG)
    state.liquidation_price = 96.0   # 4% below current → within 5%
    indicators = make_indicators(adj_close=100.0)
    config     = base_config()

    decision = Decision(action=ActionType.BUY_ADDON, qty=5, reason="test")
    allowed, reason = check_order_allowed(state, decision, config, indicators)

    assert not allowed
    assert reason == "approaching_liquidation_price"


def test_blocks_when_margin_ratio_too_high():
    """Order is blocked when margin_used / account_value > 80%."""
    state = make_state(BotState.BASE_LONG)
    state.margin_used_usd = 8500.0   # 85% of 10 000
    indicators = make_indicators()
    config     = base_config()

    decision = Decision(action=ActionType.BUY_ADDON, qty=5, reason="test")
    allowed, reason = check_order_allowed(
        state, decision, config, indicators, account_value=10_000.0
    )

    assert not allowed
    assert reason == "margin_ratio_too_high"


def test_allows_when_liq_price_far_away():
    """Order is NOT blocked by the liquidation check when liq is > 5% below current."""
    state = make_state(BotState.FLAT)
    state.liquidation_price = 79.0   # 21% below current → far away
    indicators = make_indicators(adj_close=100.0)
    config     = base_config()

    decision = Decision(action=ActionType.BUY_ADDON, qty=5, reason="test")
    _, reason = check_order_allowed(state, decision, config, indicators)

    assert reason != "approaching_liquidation_price"


def test_liquidation_halt_in_validators_when_liq_above_stop():
    """validate_data emits a HALT when liquidation_price > initial_stop_price."""
    state = make_state(BotState.BASE_LONG)
    state.liquidation_price  = 900.0
    state.initial_stop_price = 800.0   # liq > stop
    indicators = make_indicators()
    config     = base_config()

    issues = validate_data(indicators, state, config)
    liq_warns = [i for i in issues if "liquidation" in i.reason.lower()]

    assert len(liq_warns) == 1
    assert liq_warns[0].severity == "HALT"


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_no_liq_check_when_liquidation_price_is_none():
    """No liquidation block when liquidation_price is None (default)."""
    state = make_state(BotState.BASE_LONG)
    # liquidation_price defaults to None
    indicators = make_indicators(adj_close=100.0)
    config     = base_config()

    decision = Decision(action=ActionType.BUY_ADDON, qty=5, reason="test")
    _, reason = check_order_allowed(state, decision, config, indicators)

    assert reason != "approaching_liquidation_price"


def test_margin_ratio_uses_starting_equity_when_account_value_not_provided():
    """Falls back to config.capital.starting_equity when account_value is None."""
    config = base_config()
    starting = config.capital["starting_equity"]  # 100 000

    state = make_state(BotState.BASE_LONG)
    # 85% of 100 000
    state.margin_used_usd = starting * 0.85
    indicators = make_indicators()

    decision = Decision(action=ActionType.BUY_ADDON, qty=5, reason="test")
    allowed, reason = check_order_allowed(state, decision, config, indicators)
    # No explicit account_value → falls back to starting_equity
    assert not allowed
    assert reason == "margin_ratio_too_high"


def test_no_warn_when_liq_below_stop():
    """validate_data does NOT emit a liq warn when liq < stop (correct ordering)."""
    state = make_state(BotState.BASE_LONG)
    state.liquidation_price  = 700.0
    state.initial_stop_price = 800.0   # liq < stop → no warn
    indicators = make_indicators()
    config     = base_config()

    issues = validate_data(indicators, state, config)
    liq_warns = [i for i in issues if "liquidation" in i.reason.lower()]

    assert len(liq_warns) == 0


def test_liq_check_boundary_exactly_5pct():
    """At exactly 5% distance, the order is not blocked (< 0.05 required)."""
    state = make_state(BotState.FLAT)
    state.liquidation_price = 95.0   # exactly 5% below 100 → not blocked
    indicators = make_indicators(adj_close=100.0)
    config     = base_config()

    decision = Decision(action=ActionType.BUY_ADDON, qty=5, reason="test")
    _, reason = check_order_allowed(state, decision, config, indicators)

    assert reason != "approaching_liquidation_price"
