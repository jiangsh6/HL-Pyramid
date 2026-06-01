"""
Phase 5 tests — validators and risk manager (Section 6.2 / Section 26).

All 14 named tests required by the spec are present.
"""
from __future__ import annotations

import copy

from src.core.models import ActionType, BotState, Decision, ThesisState
from src.risk.risk_manager import check_order_allowed
from src.risk.validators import (
    HALT, WARN, ValidationIssue, has_halt, has_warn, validate_data,
)
from tests._helpers import base_config, make_indicators, make_lot, make_state


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean_indicators(**overrides):
    """Indicators that pass all validation checks."""
    defaults = dict(
        adj_close=100.0,
        ma5=99.0, ma10=98.0, ma20=95.0, ma50=90.0,
        atr14=2.0, avg_volume_20d=2_000_000.0,
        prior_highest_high_20d=99.0,
        volume=2_000_000.0,
    )
    defaults.update(overrides)
    return make_indicators(**defaults)


def _clean_state(**overrides):
    """State that passes all consistency checks (FLAT, no position)."""
    return make_state(state=BotState.FLAT, **overrides)


def _buy_decision(qty: int = 10) -> Decision:
    return Decision(
        action=ActionType.BUY_STARTER,
        qty=qty,
        reason="test",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Validator tests
# ─────────────────────────────────────────────────────────────────────────────

def test_nan_indicator_triggers_halt():
    """Any NaN in the checked indicator fields must produce a HALT issue."""
    cfg = base_config()
    ind = _clean_indicators(ma50=float("nan"))
    state = _clean_state()
    issues = validate_data(ind, state, cfg)
    assert has_halt(issues), "Expected a HALT issue for NaN ma50."
    halt_issues = [i for i in issues if i.severity == HALT]
    assert any("nan_indicator" in i.reason.lower() or "nan" in i.reason.lower()
               for i in halt_issues)


def test_zero_adj_close_triggers_halt():
    """adj_close = 0.0 must produce a HALT issue."""
    cfg = base_config()
    ind = _clean_indicators(adj_close=0.0)
    state = _clean_state()
    issues = validate_data(ind, state, cfg)
    assert has_halt(issues), "Expected a HALT issue for adj_close=0."
    halt_reasons = [i.reason for i in issues if i.severity == HALT]
    assert any("adj_close" in r for r in halt_reasons)


def test_negative_adj_close_triggers_halt():
    """adj_close = -5.0 must produce a HALT issue."""
    cfg = base_config()
    ind = _clean_indicators(adj_close=-5.0)
    state = _clean_state()
    issues = validate_data(ind, state, cfg)
    assert has_halt(issues), "Expected a HALT issue for adj_close=-5."
    halt_reasons = [i.reason for i in issues if i.severity == HALT]
    assert any("adj_close" in r for r in halt_reasons)


def test_state_consistency_mismatch_triggers_halt():
    """
    current_position_qty != base_lot.qty + sum(addon_lots) must HALT.
    """
    cfg = base_config()
    ind = _clean_indicators()
    # base_lot = None → expected = 0; but current_position_qty = 10 → mismatch
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=None,
        addon_lots=[],
        current_position_qty=10,   # wrong — no lots but claims 10 qty
    )
    issues = validate_data(ind, state, cfg)
    assert has_halt(issues), "Expected HALT for consistency mismatch."
    halt_reasons = [i.reason for i in issues if i.severity == HALT]
    assert any("state_consistency" in r for r in halt_reasons)


def test_flat_state_with_no_position_is_valid():
    """
    FLAT state with base_lot=None, addon_lots=[], current_position_qty=0
    must NOT produce any HALT issue.  (Confirms zero-position consistency check.)
    """
    cfg = base_config()
    ind = _clean_indicators()
    state = make_state(
        state=BotState.FLAT,
        base_lot=None,
        addon_lots=[],
        current_position_qty=0,
    )
    issues = validate_data(ind, state, cfg)
    halt_issues = [i for i in issues if i.severity == HALT]
    assert not halt_issues, (
        f"Expected no HALT for flat state with no position; got: {halt_issues}"
    )


def test_low_volume_returns_warn_not_halt():
    """
    Average daily dollar volume below min_avg_daily_dollar_volume must produce
    a WARN, not a HALT.
    """
    cfg = base_config()
    # min_avg_daily_dollar_volume = 100_000_000
    # avg_volume_20d = 500_000 qty * adj_close=100 = $50M < $100M → WARN
    ind = _clean_indicators(avg_volume_20d=500_000.0, adj_close=100.0)
    state = _clean_state()
    issues = validate_data(ind, state, cfg)
    assert has_warn(issues), "Expected a WARN for low dollar volume."
    assert not has_halt(issues), "Low volume must NOT trigger HALT."
    warn_reasons = [i.reason for i in issues if i.severity == WARN]
    assert any("volume" in r.lower() for r in warn_reasons)


def test_clean_state_returns_no_issues():
    """Valid indicators + consistent state → empty issue list."""
    cfg = base_config()
    ind = _clean_indicators()
    state = _clean_state()
    issues = validate_data(ind, state, cfg)
    assert issues == [], f"Expected no issues for clean state; got: {issues}"


# ─────────────────────────────────────────────────────────────────────────────
# Risk manager tests
# ─────────────────────────────────────────────────────────────────────────────

def test_risk_manager_blocks_when_halted():
    """state = HALTED → check_order_allowed returns (False, ...)."""
    cfg = base_config()
    state = make_state(state=BotState.HALTED, halted=True, halt_reason="max_thesis_loss")
    decision = _buy_decision()
    ind = _clean_indicators()
    allowed, reason = check_order_allowed(state, decision, cfg, ind)
    assert allowed is False
    assert "halted" in reason.lower()


def test_risk_manager_blocks_when_exited():
    """state = EXITED → check_order_allowed returns (False, ...)."""
    cfg = base_config()
    state = make_state(state=BotState.EXITED)
    decision = _buy_decision()
    ind = _clean_indicators()
    allowed, reason = check_order_allowed(state, decision, cfg, ind)
    assert allowed is False
    assert "exited" in reason.lower()


def test_risk_manager_blocks_when_daily_loss_limit_hit():
    """
    daily_pnl / starting_equity <= -max_daily_loss_pct_of_equity (3%)
    → check_order_allowed returns (False, ...).
    """
    cfg = base_config()
    # starting = 100_000, max_daily_loss = 0.03 → threshold = -$3_000
    state = make_state(daily_pnl=-3_100.0)   # exceeds 3%
    decision = _buy_decision()
    ind = _clean_indicators()
    allowed, reason = check_order_allowed(state, decision, cfg, ind)
    assert allowed is False
    assert "daily_loss" in reason.lower()


def test_risk_manager_blocks_when_exposure_would_exceed_max():
    """
    Proposed buy that would push exposure above max_symbol_exposure_pct (1.0)
    must be blocked.
    """
    cfg = base_config()
    # current: 900 qty × $100 = $90_000 = 90% of 100_000
    # buy 200 more × $100 = $20_000 → 110% > 100% → block
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 900),
        current_position_qty=900,
        avg_entry_price=100.0,
    )
    decision = Decision(action=ActionType.BUY_ADDON, qty=200, reason="test")
    ind = _clean_indicators(adj_close=100.0)
    allowed, reason = check_order_allowed(state, decision, cfg, ind)
    assert allowed is False
    assert "exposure" in reason.lower()


def test_risk_manager_allows_valid_order():
    """Clean state + valid order → check_order_allowed returns (True, 'ok')."""
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 10),
        current_position_qty=10,
        avg_entry_price=100.0,
    )
    # 10 current + 5 proposed = 15 qty × $100 = $1_500 / $100_000 = 1.5% ≪ 100%
    decision = Decision(action=ActionType.BUY_ADDON, qty=5, reason="test")
    ind = _clean_indicators(adj_close=100.0)
    allowed, reason = check_order_allowed(state, decision, cfg, ind)
    assert allowed is True
    assert reason == "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Halt-never-auto-cleared tests (safety invariants)
# ─────────────────────────────────────────────────────────────────────────────

def test_halt_never_auto_cleared_by_validators():
    """
    Calling validate_data on a HALTED state with otherwise-valid data must
    not clear state.halted or change state.state.
    Confirms validators are pure and never write state.halted = False.
    """
    cfg = base_config()
    ind = _clean_indicators()
    state = make_state(
        state=BotState.HALTED,
        halted=True,
        halt_reason="max_thesis_loss",
    )
    # Capture state before
    state_before = state.state
    halted_before = state.halted
    halt_reason_before = state.halt_reason

    _ = validate_data(ind, state, cfg)   # call validators

    # State must be completely unchanged
    assert state.state   == state_before,       "validate_data must not change state.state"
    assert state.halted  == halted_before,       "validate_data must not clear state.halted"
    assert state.halt_reason == halt_reason_before, "validate_data must not change halt_reason"


def test_halt_never_auto_cleared_by_risk_manager():
    """
    Calling check_order_allowed on a HALTED state must return (False, ...)
    without mutating state.halted, state.state, or state.halt_reason.
    Confirms risk_manager is pure and never auto-clears a halt.
    """
    cfg = base_config()
    ind = _clean_indicators()
    state = make_state(
        state=BotState.HALTED,
        halted=True,
        halt_reason="state_consistency_error",
    )
    decision = _buy_decision()

    state_before     = state.state
    halted_before    = state.halted
    halt_reason_before = state.halt_reason

    allowed, reason = check_order_allowed(state, decision, cfg, ind)

    # Must block
    assert allowed is False, "risk_manager must block orders in HALTED state"
    assert "halted" in reason.lower()

    # Must not mutate state
    assert state.state      == state_before,        "check_order_allowed must not change state.state"
    assert state.halted     == halted_before,        "check_order_allowed must not clear state.halted"
    assert state.halt_reason == halt_reason_before,  "check_order_allowed must not change halt_reason"
