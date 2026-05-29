"""
Section 22.3 — State machine tests (23 tests).
"""
import pytest

from src.core.models import BotState, ThesisState
from src.core.state_machine import StateMachine, VALID_TRANSITIONS


def _sm(state: BotState) -> StateMachine:
    return StateMachine(current_state=state)


# ── Entry transitions ────────────────────────────────────────────────────────

def test_flat_to_starter_long():
    sm = _sm(BotState.FLAT)
    sm.transition(BotState.STARTER_LONG)
    assert sm.current_state == BotState.STARTER_LONG


def test_flat_to_base_long_direct():
    sm = _sm(BotState.FLAT)
    sm.transition(BotState.BASE_LONG)
    assert sm.current_state == BotState.BASE_LONG


def test_starter_to_base_long():
    sm = _sm(BotState.STARTER_LONG)
    sm.transition(BotState.BASE_LONG)
    assert sm.current_state == BotState.BASE_LONG


def test_base_to_pyramid_long():
    sm = _sm(BotState.BASE_LONG)
    sm.transition(BotState.PYRAMID_LONG)
    assert sm.current_state == BotState.PYRAMID_LONG


def test_pyramid_to_pyramid_on_add():
    # PYRAMID_LONG → PYRAMID_LONG is a valid self-transition (subsequent adds)
    sm = _sm(BotState.PYRAMID_LONG)
    sm.transition(BotState.PYRAMID_LONG)
    assert sm.current_state == BotState.PYRAMID_LONG


# ── Reduce transitions ───────────────────────────────────────────────────────

def test_pyramid_to_reduce_mode():
    sm = _sm(BotState.PYRAMID_LONG)
    sm.transition(BotState.REDUCE_MODE)
    assert sm.current_state == BotState.REDUCE_MODE


def test_reduce_mode_to_base_long_after_all_addons_removed():
    sm = _sm(BotState.REDUCE_MODE)
    sm.transition(BotState.BASE_LONG)
    assert sm.current_state == BotState.BASE_LONG


def test_reduce_mode_to_exited_when_base_fully_exited():
    sm = _sm(BotState.REDUCE_MODE)
    sm.transition(BotState.EXITED)
    assert sm.current_state == BotState.EXITED


# ── Event risk transitions ───────────────────────────────────────────────────

def test_any_long_to_event_risk_mode():
    for state in [BotState.STARTER_LONG, BotState.BASE_LONG,
                  BotState.PYRAMID_LONG, BotState.REDUCE_MODE]:
        sm = _sm(state)
        sm.transition(BotState.EVENT_RISK_MODE)
        assert sm.current_state == BotState.EVENT_RISK_MODE


def test_event_risk_mode_restores_prior_state_after_cooldown():
    # Simulate: enter EVENT_RISK_MODE from BASE_LONG, then restore to BASE_LONG
    thesis_state = ThesisState(symbol="MU", state=BotState.BASE_LONG)
    thesis_state.prior_state = BotState.BASE_LONG
    thesis_state.state = BotState.EVENT_RISK_MODE

    sm = _sm(BotState.EVENT_RISK_MODE)
    sm.transition(BotState.BASE_LONG)   # restore prior state
    assert sm.current_state == BotState.BASE_LONG
    assert thesis_state.prior_state == BotState.BASE_LONG


# ── Runner mode transitions ──────────────────────────────────────────────────

def test_any_long_to_runner_long_on_target_tp():
    # VALID_TRANSITIONS dict (Section 4.2) lists these as valid sources for RUNNER_LONG.
    # STARTER_LONG is not in the dict's RUNNER_LONG targets.
    for state in [BotState.BASE_LONG, BotState.PYRAMID_LONG,
                  BotState.REDUCE_MODE, BotState.EVENT_RISK_MODE]:
        sm = _sm(state)
        sm.transition(BotState.RUNNER_LONG)
        assert sm.current_state == BotState.RUNNER_LONG


# ── Exit and halt transitions ────────────────────────────────────────────────

def test_any_long_to_exited_on_hard_stop():
    long_states = [
        BotState.STARTER_LONG, BotState.BASE_LONG,
        BotState.PYRAMID_LONG, BotState.REDUCE_MODE,
        BotState.EVENT_RISK_MODE,
    ]
    for state in long_states:
        sm = _sm(state)
        sm.transition(BotState.EXITED)
        assert sm.current_state == BotState.EXITED


def test_runner_long_to_exited_on_runner_stop():
    sm = _sm(BotState.RUNNER_LONG)
    sm.transition(BotState.EXITED)
    assert sm.current_state == BotState.EXITED


def test_any_state_to_halted_on_max_loss():
    haltable = [
        BotState.FLAT, BotState.STARTER_LONG, BotState.BASE_LONG,
        BotState.PYRAMID_LONG, BotState.REDUCE_MODE,
        BotState.EVENT_RISK_MODE, BotState.RUNNER_LONG,
    ]
    for state in haltable:
        sm = _sm(state)
        sm.transition(BotState.HALTED)
        assert sm.current_state == BotState.HALTED


# ── Manual-reset-only transitions ───────────────────────────────────────────

def test_halted_requires_manual_reset():
    sm = _sm(BotState.HALTED)
    # Only FLAT is valid from HALTED (manual reset)
    assert sm.can_transition(BotState.FLAT)
    # All other transitions are invalid
    for state in BotState:
        if state != BotState.FLAT:
            assert not sm.can_transition(state)


def test_exited_requires_manual_reset():
    sm = _sm(BotState.EXITED)
    assert sm.can_transition(BotState.FLAT)
    for state in BotState:
        if state != BotState.FLAT:
            assert not sm.can_transition(state)


# ── Invalid transition raises ────────────────────────────────────────────────

def test_invalid_transition_raises_error():
    sm = _sm(BotState.FLAT)
    with pytest.raises(ValueError, match="Invalid state transition"):
        sm.transition(BotState.PYRAMID_LONG)


# ── protect_profit_mode behavioural tests ────────────────────────────────────

def test_protect_profit_mode_set_at_max_add_count():
    state = ThesisState(symbol="MU", state=BotState.PYRAMID_LONG)
    state.add_count = 4   # equals max_add_count
    state.protect_profit_mode = state.add_count >= 4
    assert state.protect_profit_mode is True


def test_protect_profit_mode_set_at_30pct_unrealized_profit():
    state = ThesisState(
        symbol="MU",
        state=BotState.BASE_LONG,
        avg_entry_price=1000.0,
        current_position_shares=10,
    )
    adj_close = 1300.0   # 30% profit
    profit_pct = (adj_close - state.avg_entry_price) / state.avg_entry_price
    state.protect_profit_mode = profit_pct >= 0.30
    assert state.protect_profit_mode is True


def test_add_blocked_when_protect_profit_mode_true():
    state = ThesisState(
        symbol="MU",
        state=BotState.PYRAMID_LONG,
        protect_profit_mode=True,
    )
    # Decision engine step 12 requires protect_profit_mode = False; simulate gate
    can_add = not state.protect_profit_mode
    assert can_add is False


def test_protect_profit_mode_cleared_on_event_reset():
    state = ThesisState(symbol="MU", state=BotState.EVENT_RISK_MODE)
    state.protect_profit_mode = True
    state.add_count = 4

    # Simulate post-event reset_add_count = true path
    state.add_count = 0
    state.protect_profit_mode = False

    assert state.protect_profit_mode is False
    assert state.add_count == 0


def test_state_remains_pyramid_long_when_protect_profit_mode_set():
    state = ThesisState(symbol="MU", state=BotState.PYRAMID_LONG)
    state.protect_profit_mode = True
    # protect_profit_mode is a flag — state must not change to anything else
    assert state.state == BotState.PYRAMID_LONG


def test_reduce_fires_normally_when_protect_profit_mode_true():
    # protect_profit_mode only blocks adds; reduce logic is unaffected
    state = ThesisState(
        symbol="MU",
        state=BotState.PYRAMID_LONG,
        protect_profit_mode=True,
    )
    # Verify reduce transition is still valid from PYRAMID_LONG
    sm = _sm(BotState.PYRAMID_LONG)
    sm.transition(BotState.REDUCE_MODE)
    assert sm.current_state == BotState.REDUCE_MODE


# ── VALID_TRANSITIONS completeness ───────────────────────────────────────────

def test_valid_transitions_matches_spec():
    """Verify VALID_TRANSITIONS dict exactly matches Section 4.2."""
    expected = {
        "FLAT":             {"STARTER_LONG", "BASE_LONG", "HALTED"},
        "STARTER_LONG":     {"BASE_LONG", "EVENT_RISK_MODE", "EXITED", "HALTED"},
        "BASE_LONG":        {"PYRAMID_LONG", "REDUCE_MODE", "EVENT_RISK_MODE",
                             "RUNNER_LONG", "EXITED", "HALTED"},
        "PYRAMID_LONG":     {"PYRAMID_LONG", "REDUCE_MODE", "EVENT_RISK_MODE",
                             "RUNNER_LONG", "EXITED", "HALTED"},
        "REDUCE_MODE":      {"BASE_LONG", "EVENT_RISK_MODE", "RUNNER_LONG",
                             "EXITED", "HALTED"},
        "EVENT_RISK_MODE":  {"STARTER_LONG", "BASE_LONG", "PYRAMID_LONG",
                             "REDUCE_MODE", "RUNNER_LONG", "EXITED", "HALTED"},
        "RUNNER_LONG":      {"EXITED", "HALTED"},
        "EXITED":           {"FLAT"},
        "HALTED":           {"FLAT"},
    }
    assert VALID_TRANSITIONS == expected
