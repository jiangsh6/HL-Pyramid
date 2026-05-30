"""
Position reconciler — Phase 3.

reconcile(state, hl_snapshot, config) -> ReconcileResult
update_state_from_hl(state, hl_pos) -> ThesisState

Compares bot internal state to the live HL account snapshot and
detects drift, mismatch, or liquidation events.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.core.models import BotConfig, ThesisState
from src.hl.account import HLAccountSnapshot, HLPosition


@dataclass
class ReconcileResult:
    status: str                       # "ok" | "warn" | "halt"
    drift_contracts: float
    reason: Optional[str] = None
    recommended_action: Optional[str] = None


def reconcile(
    state: ThesisState,
    hl_snapshot: HLAccountSnapshot,
    config: BotConfig,
) -> ReconcileResult:
    """
    Compare bot state vs HL account snapshot.

    Decision logic (in evaluation order):
      1. Both flat → ok
      2. HL=0 but state>0 → halt (liquidation_detected)
      3. Exact match → ok
      4. drift < min_size*2 → warn (minor_drift_within_tolerance)
      5. drift >= min_size*2 → halt (position_mismatch_exceeds_tolerance)
    """
    min_size: float = float(config.hl.get("min_size", 0.001)) if config.hl else 0.001
    tolerance: float = min_size * 2.0

    coin: str = config.hl["coin"] if config.hl else ""
    state_contracts: float = state.current_position_contracts

    hl_pos = next((p for p in hl_snapshot.positions if p.coin == coin), None)
    hl_contracts: float = hl_pos.contracts if hl_pos is not None else 0.0

    # Case 1: both flat
    if state_contracts == 0.0 and hl_contracts == 0.0:
        return ReconcileResult(status="ok", drift_contracts=0.0)

    # Case 2: HL shows 0 but state is long → likely liquidated
    if hl_contracts == 0.0 and state_contracts > 0.0:
        return ReconcileResult(
            status="halt",
            drift_contracts=state_contracts,
            reason="liquidation_detected",
            recommended_action="reset_state_and_investigate",
        )

    drift: float = abs(hl_contracts - state_contracts)

    # Exact match (float epsilon)
    if drift < 1e-9:
        return ReconcileResult(status="ok", drift_contracts=0.0)

    if drift < tolerance:
        return ReconcileResult(
            status="warn",
            drift_contracts=drift,
            reason="minor_drift_within_tolerance",
            recommended_action="monitor",
        )

    return ReconcileResult(
        status="halt",
        drift_contracts=drift,
        reason="position_mismatch_exceeds_tolerance",
        recommended_action="manual_reconciliation_required",
    )


def update_state_from_hl(state: ThesisState, hl_pos: HLPosition) -> ThesisState:
    """
    Update liquidation and margin metadata from live HL position.

    Observes only — does NOT alter lot structure.
    Returns the (mutated) state object.
    """
    state.liquidation_price = hl_pos.liquidation_price
    state.margin_used_usd   = hl_pos.margin_used
    state.leverage_used     = hl_pos.leverage
    return state
