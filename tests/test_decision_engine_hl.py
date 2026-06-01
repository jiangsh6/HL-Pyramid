"""
HL Phase 3 — Decision engine integration tests for HL snapshot parameter.

All 4 named tests:
  test_decision_engine_accepts_hl_snapshot_parameter
  test_decision_engine_halts_on_reconcile_halt
  test_decision_engine_continues_on_reconcile_warn
  test_decision_engine_skips_hl_reconcile_in_paper_mode
"""
from __future__ import annotations

from datetime import date
import os
from unittest.mock import patch

import pytest

from src.core.config_loader import load_config
from src.core.decision_engine import run
from src.core.models import ActionType, BotState, LotRecord, ThesisState
from src.hl.account import HLAccountSnapshot, HLPosition
from tests._helpers import base_config, make_indicators, make_state


HL_CONFIG_PATH = "config/btc_long_thesis.yaml"
TESTNET_WALLET = "0x1111111111111111111111111111111111111111"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hl_config():
    with patch.dict(os.environ, {"HL_TESTNET_ACCOUNT_ADDRESS": TESTNET_WALLET}, clear=False):
        return load_config(HL_CONFIG_PATH)


def _make_hl_snapshot(
    contracts: float = 0.0,
    coin: str = "BTC",
    liq_px=None,
) -> HLAccountSnapshot:
    positions = []
    if contracts > 0.0:
        positions.append(HLPosition(
            coin=coin,
            qty=contracts,
            entry_price=50000.0,
            mark_price=50000.0,
            unrealized_pnl=0.0,
            liquidation_price=liq_px,
            margin_used=1000.0,
            leverage=3.0,
        ))
    return HLAccountSnapshot(
        account_value=10000.0,
        margin_used=1000.0 if contracts > 0 else 0.0,
        withdrawable=9000.0,
        positions=positions,
    )


def _make_hl_state(contracts: float) -> ThesisState:
    if contracts > 0:
        lot = LotRecord(
            lot_id="base",
            entry_price=50000.0,
            qty=contracts,
            entry_date=date(2026, 1, 1),
        )
        return ThesisState(
            symbol="BTC",
            state=BotState.BASE_LONG,
            base_lot=lot,
            current_position_qty=contracts,
        )
    return ThesisState(symbol="BTC", state=BotState.FLAT)


# ── Named tests ───────────────────────────────────────────────────────────────

def test_decision_engine_accepts_hl_snapshot_parameter():
    """run() accepts an optional hl_snapshot without raising."""
    config     = base_config()   # yfinance / paper config
    state      = make_state(BotState.FLAT)
    indicators = make_indicators()
    snapshot   = HLAccountSnapshot(
        account_value=10000.0,
        margin_used=0.0,
        withdrawable=10000.0,
        positions=[],
    )

    decision = run(state, indicators, config, hl_snapshot=snapshot)

    assert decision is not None


def test_decision_engine_halts_on_reconcile_halt():
    """Engine returns HALT when reconcile detects liquidation (HL=0, state>0)."""
    config = _hl_config()
    state  = _make_hl_state(0.1)   # state thinks 0.1 contracts open

    indicators = make_indicators()
    snapshot   = _make_hl_snapshot(0.0)   # HL shows flat → liquidation

    decision = run(state, indicators, config, hl_snapshot=snapshot)

    assert decision.action == ActionType.HALT
    assert decision.reason == "liquidation_detected"


def test_decision_engine_continues_on_reconcile_warn():
    """Engine does NOT halt when reconcile returns warn (minor drift)."""
    config = _hl_config()
    state  = _make_hl_state(0.1)

    indicators = make_indicators()
    # Drift = 0.0009 < tolerance 0.002 → warn
    snapshot = _make_hl_snapshot(0.1009)

    decision = run(state, indicators, config, hl_snapshot=snapshot)

    assert decision.action != ActionType.HALT


def test_decision_engine_skips_hl_reconcile_in_paper_mode():
    """With data.source='yfinance', HL snapshot is ignored (no reconcile halt)."""
    config     = base_config()   # yfinance config
    state      = make_state(BotState.FLAT)
    indicators = make_indicators()

    # Snapshot has a mismatched position — would halt if reconcile ran
    snapshot = HLAccountSnapshot(
        account_value=10000.0,
        margin_used=5000.0,
        withdrawable=5000.0,
        positions=[
            HLPosition(
                coin="MU",
                qty=100.0,   # state has 0 → would be liquidation_detected
                entry_price=100.0,
                mark_price=100.0,
                unrealized_pnl=0.0,
                liquidation_price=None,
                margin_used=5000.0,
                leverage=1.0,
            )
        ],
    )

    decision = run(state, indicators, config, hl_snapshot=snapshot)

    assert decision.action != ActionType.HALT


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_decision_engine_run_unchanged_without_hl_snapshot():
    """Existing call sites (no hl_snapshot kwarg) still work normally."""
    config     = base_config()
    state      = make_state(BotState.FLAT)
    indicators = make_indicators()

    decision = run(state, indicators, config)   # no hl_snapshot

    assert decision is not None


def test_decision_engine_updates_state_liquidation_price_from_snapshot():
    """After ok reconcile, state.liquidation_price is populated from HL position."""
    config = _hl_config()
    state  = _make_hl_state(0.1)

    indicators = make_indicators()
    snapshot   = _make_hl_snapshot(0.1, liq_px=40000.0)

    run(state, indicators, config, hl_snapshot=snapshot)

    assert state.liquidation_price == 40000.0


def test_decision_engine_halt_sets_state_to_halted():
    """On reconcile halt, the ThesisState.state is set to HALTED."""
    config = _hl_config()
    state  = _make_hl_state(0.1)

    indicators = make_indicators()
    snapshot   = _make_hl_snapshot(0.0)   # liquidation

    run(state, indicators, config, hl_snapshot=snapshot)

    assert state.state   == BotState.HALTED
    assert state.halted  is True
    assert state.halt_reason == "liquidation_detected"
