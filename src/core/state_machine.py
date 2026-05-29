from __future__ import annotations

from .models import BotState

VALID_TRANSITIONS: dict[str, set[str]] = {
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


class StateMachine:
    """
    Validates and executes BotState transitions.
    Does not hold any position or PnL data — transitions only.
    """

    def __init__(self, current_state: BotState = BotState.FLAT) -> None:
        self.current_state = current_state

    def transition(self, new_state: BotState) -> BotState:
        """
        Transition to new_state.
        Raises ValueError if the transition is not in VALID_TRANSITIONS.
        Self-transitions (same → same) are permitted only when the transition
        table includes the state in its own target set (e.g. PYRAMID_LONG → PYRAMID_LONG).
        """
        from_key = self.current_state.value
        to_key   = new_state.value

        allowed = VALID_TRANSITIONS.get(from_key, set())
        if to_key not in allowed:
            raise ValueError(
                f"Invalid state transition: {from_key} → {to_key}. "
                f"Allowed targets from {from_key}: {sorted(allowed)}"
            )

        self.current_state = new_state
        return self.current_state

    def can_transition(self, new_state: BotState) -> bool:
        """Return True if the transition would be valid without raising."""
        from_key = self.current_state.value
        to_key   = new_state.value
        return to_key in VALID_TRANSITIONS.get(from_key, set())
