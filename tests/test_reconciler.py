"""
HL Phase 3 — Position reconciler tests.

All 7 named tests:
  test_reconcile_ok_when_both_flat
  test_reconcile_ok_when_positions_match
  test_reconcile_warn_on_minor_drift_within_tolerance
  test_reconcile_halt_on_major_drift
  test_reconcile_halt_on_liquidation_detected
  test_update_state_from_hl_updates_liquidation_price
  test_update_state_from_hl_does_not_change_lot_structure
"""
from __future__ import annotations

from datetime import date

import pytest

from src.core.config_loader import load_config
from src.core.models import BotState, LotRecord, ThesisState
from src.hl.account import HLAccountSnapshot, HLPosition
from src.hl.position_reconciler import ReconcileResult, reconcile, update_state_from_hl
from tests._helpers import make_state


HL_CONFIG_PATH = "config/btc_long_thesis.yaml"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hl_config():
    return load_config(HL_CONFIG_PATH)


def _make_snapshot(coin: str = "BTC", contracts: float = 0.0,
                   liq_px=None) -> HLAccountSnapshot:
    positions = []
    if contracts != 0.0:
        positions.append(HLPosition(
            coin=coin,
            contracts=contracts,
            entry_price=50000.0,
            mark_price=50000.0,
            unrealized_pnl=0.0,
            liquidation_price=liq_px,
            margin_used=1000.0,
            leverage=3.0,
        ))
    return HLAccountSnapshot(
        account_value=10000.0,
        margin_used=1000.0 if contracts != 0.0 else 0.0,
        withdrawable=9000.0,
        positions=positions,
    )


def _make_hl_state(contracts: float) -> ThesisState:
    """Make a ThesisState with a base lot matching the given contract size."""
    if contracts > 0:
        lot = LotRecord(
            lot_id="base",
            entry_price=50000.0,
            contracts=contracts,
            entry_date=date(2026, 1, 1),
        )
        return ThesisState(
            symbol="BTC",
            state=BotState.BASE_LONG,
            base_lot=lot,
            current_position_contracts=contracts,
        )
    return ThesisState(symbol="BTC", state=BotState.FLAT)


# ── Named tests ───────────────────────────────────────────────────────────────

def test_reconcile_ok_when_both_flat():
    """Both state and HL show 0 contracts → status ok."""
    config   = _hl_config()
    state    = _make_hl_state(0.0)
    snapshot = _make_snapshot("BTC", 0.0)

    result = reconcile(state, snapshot, config)

    assert result.status == "ok"
    assert result.drift_contracts == 0.0


def test_reconcile_ok_when_positions_match():
    """Exact match between state contracts and HL contracts → status ok."""
    config   = _hl_config()
    state    = _make_hl_state(0.1)
    snapshot = _make_snapshot("BTC", 0.1)

    result = reconcile(state, snapshot, config)

    assert result.status == "ok"


def test_reconcile_warn_on_minor_drift_within_tolerance():
    """Drift < min_size*2 (0.002) → status warn."""
    config   = _hl_config()
    state    = _make_hl_state(0.1)
    # Drift = |0.1009 - 0.1| = 0.0009 < 0.002 (min_size=0.001, tolerance=0.002)
    snapshot = _make_snapshot("BTC", 0.1009)

    result = reconcile(state, snapshot, config)

    assert result.status == "warn"
    assert result.reason == "minor_drift_within_tolerance"
    assert result.drift_contracts == pytest.approx(0.0009, abs=1e-9)


def test_reconcile_halt_on_major_drift():
    """Drift >= min_size*2 → status halt with position_mismatch_exceeds_tolerance."""
    config   = _hl_config()
    state    = _make_hl_state(0.1)
    # Drift = |0.103 - 0.1| = 0.003 >= 0.002
    snapshot = _make_snapshot("BTC", 0.103)

    result = reconcile(state, snapshot, config)

    assert result.status == "halt"
    assert result.reason == "position_mismatch_exceeds_tolerance"
    assert result.drift_contracts == pytest.approx(0.003, abs=1e-9)


def test_reconcile_halt_on_liquidation_detected():
    """HL shows 0 contracts but state shows > 0 → halt with liquidation_detected."""
    config   = _hl_config()
    state    = _make_hl_state(0.1)
    snapshot = _make_snapshot("BTC", 0.0)  # HL is flat

    result = reconcile(state, snapshot, config)

    assert result.status == "halt"
    assert result.reason == "liquidation_detected"
    assert result.drift_contracts == pytest.approx(0.1)


def test_update_state_from_hl_updates_liquidation_price():
    """update_state_from_hl writes liquidation_price and margin_used_usd to state."""
    state  = make_state(BotState.BASE_LONG)
    hl_pos = HLPosition(
        coin="BTC",
        contracts=0.1,
        entry_price=50000.0,
        mark_price=51000.0,
        unrealized_pnl=100.0,
        liquidation_price=45000.0,
        margin_used=1000.0,
        leverage=3.0,
    )

    updated = update_state_from_hl(state, hl_pos)

    assert updated.liquidation_price == 45000.0
    assert updated.margin_used_usd   == 1000.0
    assert updated.leverage_used     == 3.0


def test_update_state_from_hl_does_not_change_lot_structure():
    """update_state_from_hl never alters base_lot or addon_lots."""
    lot = LotRecord(
        lot_id="base",
        entry_price=50000.0,
        contracts=0.1,
        entry_date=date(2026, 1, 1),
    )
    state = ThesisState(
        symbol="BTC",
        state=BotState.BASE_LONG,
        base_lot=lot,
        addon_lots=[],
        current_position_contracts=0.1,
    )
    hl_pos = HLPosition(
        coin="BTC",
        contracts=0.2,        # different from state — reconciler only observes
        entry_price=48000.0,
        mark_price=51000.0,
        unrealized_pnl=300.0,
        liquidation_price=43000.0,
        margin_used=2000.0,
        leverage=5.0,
    )

    updated = update_state_from_hl(state, hl_pos)

    assert updated.base_lot.contracts  == 0.1
    assert updated.base_lot.entry_price == 50000.0
    assert len(updated.addon_lots)     == 0
    assert updated.current_position_contracts == 0.1
    # But metadata is updated
    assert updated.liquidation_price == 43000.0
    assert updated.margin_used_usd   == 2000.0


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_reconcile_uses_min_size_from_config():
    """min_size is read from config.hl.min_size; drift clearly above tolerance → halt."""
    config = _hl_config()
    assert config.hl["min_size"] == 0.001  # BTC config default

    state    = _make_hl_state(0.1)
    # Drift = |0.105 - 0.1| = 0.005 >= 0.002 (tolerance) → halt
    snapshot = _make_snapshot("BTC", 0.105)

    result = reconcile(state, snapshot, config)

    assert result.status == "halt"


def test_update_state_from_hl_handles_none_liquidation_price():
    """None liquidation_price from HL is stored as None on state."""
    state  = make_state(BotState.BASE_LONG)
    hl_pos = HLPosition(
        coin="BTC",
        contracts=0.1,
        entry_price=50000.0,
        mark_price=50000.0,
        unrealized_pnl=0.0,
        liquidation_price=None,
        margin_used=500.0,
        leverage=2.0,
    )

    updated = update_state_from_hl(state, hl_pos)

    assert updated.liquidation_price is None
