"""
Paper broker + state_writer + reset tests.

Covers Section 18 (paper broker fill model), Section 7 (state.json round-trip),
and Section 25.3 / 23.13 (reset_state --confirm requirement).
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from src.core.models import (
    ActionType, BotState, Decision, Fill, LotRecord, ThesisState,
)
from src.execution.paper_broker import execute
from src.reporting.logger import log_cycle
from src.reporting.state_writer import (
    apply_fill, read_state, reset_state_to_flat, write_state,
)
from src.reporting.daily_summary import format_summary
from tests._helpers import base_config, make_indicators, make_lot, make_state


# ── paper_broker.execute() ───────────────────────────────────────────────────

def test_paper_broker_is_stateless():
    """execute() must not modify state (Section 18.1)."""
    random.seed(0)
    cfg = base_config()
    state = make_state()
    state_snapshot = state.model_dump()
    decision = Decision(action=ActionType.BUY_STARTER, shares=10, reason="t")
    execute(decision, fill_ref_price=900.0, state=state, config=cfg)
    assert state.model_dump() == state_snapshot


def test_paper_broker_buy_applies_positive_slippage():
    random.seed(0)
    cfg = base_config()
    state = make_state()
    decision = Decision(action=ActionType.BUY_STARTER, shares=10, reason="t")
    fill = execute(decision, fill_ref_price=100.0, state=state, config=cfg)
    assert fill.fill_price >= 100.0
    assert 0.0 <= fill.slippage_bps <= cfg.execution["max_slippage_bps"] + 1e-6


def test_paper_broker_sell_applies_negative_slippage():
    random.seed(0)
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 10),
        avg_entry_price=100.0,
        current_position_shares=10,
    )
    decision = Decision(action=ActionType.SELL_STOP, shares=10, reason="t")
    fill = execute(decision, fill_ref_price=120.0, state=state, config=cfg)
    assert fill.fill_price <= 120.0


def test_paper_broker_commission_is_zero():
    cfg = base_config()
    state = make_state()
    decision = Decision(action=ActionType.BUY_STARTER, shares=10, reason="t")
    fill = execute(decision, fill_ref_price=100.0, state=state, config=cfg)
    assert fill.commission == 0.0


def test_paper_broker_buy_realized_pnl_is_zero():
    cfg = base_config()
    state = make_state()
    decision = Decision(action=ActionType.BUY_STARTER, shares=10, reason="t")
    fill = execute(decision, fill_ref_price=100.0, state=state, config=cfg)
    assert fill.realized_pnl == 0.0


def test_paper_broker_sell_realized_pnl_uses_lifo_cost_basis():
    cfg = base_config()
    cfg.execution["max_slippage_bps"] = 0   # deterministic
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 100.0, 10),
        addon_lots=[make_lot("add_1", 110.0, 5)],
        avg_entry_price=103.33,
        current_position_shares=15,
    )
    decision = Decision(action=ActionType.SELL_TAKE_PROFIT, shares=5, reason="t")
    fill = execute(decision, fill_ref_price=130.0, state=state, config=cfg)
    # LIFO: 5 shares from add_1 @ 110 → realized = (130-110)*5 = 100
    assert abs(fill.realized_pnl - 100.0) < 1e-6


def test_paper_broker_sell_spans_addons_and_base():
    cfg = base_config()
    cfg.execution["max_slippage_bps"] = 0
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 100.0, 10),
        addon_lots=[make_lot("add_1", 110.0, 4)],
        avg_entry_price=102.85,
        current_position_shares=14,
    )
    # Sell 8 → 4 from add_1 @110 + 4 from base @100
    decision = Decision(action=ActionType.EXIT_ALL, shares=8, reason="t")
    fill = execute(decision, fill_ref_price=130.0, state=state, config=cfg)
    expected = (130 - 110) * 4 + (130 - 100) * 4
    assert abs(fill.realized_pnl - expected) < 1e-6


def test_paper_broker_slippage_bounded_by_max():
    """Run many fills; slippage_bps must stay within [0, max_slippage_bps]."""
    cfg = base_config()
    state = make_state()
    decision = Decision(action=ActionType.BUY_STARTER, shares=1, reason="t")
    max_bps = cfg.execution["max_slippage_bps"]
    for _ in range(200):
        fill = execute(decision, fill_ref_price=100.0, state=state, config=cfg)
        assert 0.0 <= fill.slippage_bps <= max_bps + 1e-6


# ── state_writer.apply_fill() ────────────────────────────────────────────────

def _fill(action, shares, fill_price, realized_pnl=0.0, ts_date="2026-05-27"):
    return Fill(
        action=action, shares=shares, fill_price=fill_price,
        slippage_bps=0.0, realized_pnl=realized_pnl, commission=0.0,
        timestamp=datetime.fromisoformat(f"{ts_date}T16:00:00"),
    )


def test_apply_fill_buy_starter_creates_base_lot():
    state = make_state()
    fill = _fill(ActionType.BUY_STARTER, shares=10, fill_price=100.0)
    apply_fill(state, fill)
    assert state.base_lot is not None
    assert state.base_lot.shares == 10
    assert state.base_lot.entry_price == 100.0
    assert state.current_position_shares == 10
    assert state.avg_entry_price == 100.0
    assert state.last_add_price == 100.0
    assert state.entry_date == date(2026, 5, 27)


def test_apply_fill_buy_base_combines_with_existing_starter():
    state = make_state(
        state=BotState.STARTER_LONG,
        base_lot=make_lot("base", 100.0, 5),
        avg_entry_price=100.0,
        current_position_shares=5,
        entry_date=date(2026, 5, 20),
    )
    fill = _fill(ActionType.BUY_BASE, shares=10, fill_price=110.0)
    apply_fill(state, fill)
    # Combined: 5@100 + 10@110 = 15 shares; avg = (500+1100)/15 = 106.667
    assert state.base_lot.shares == 15
    assert abs(state.base_lot.entry_price - 106.6667) < 0.01
    assert state.current_position_shares == 15
    assert state.entry_date == date(2026, 5, 20)   # original kept


def test_apply_fill_buy_addon_appends_and_increments_count():
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 10),
        avg_entry_price=100.0,
        current_position_shares=10,
    )
    fill = _fill(ActionType.BUY_ADDON, shares=5, fill_price=110.0)
    apply_fill(state, fill)
    assert len(state.addon_lots) == 1
    assert state.addon_lots[0].lot_id == "add_1"
    assert state.addon_lots[0].shares == 5
    assert state.add_count == 1
    assert state.last_add_price == 110.0
    assert state.current_position_shares == 15


def test_apply_fill_sell_lifo_reduces_addons_first():
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 100.0, 10),
        addon_lots=[
            make_lot("add_1", 110.0, 4),
            make_lot("add_2", 120.0, 6),
        ],
        avg_entry_price=110.0,
        current_position_shares=20,
    )
    fill = _fill(ActionType.SELL_TAKE_PROFIT, shares=6,
                 fill_price=130.0, realized_pnl=60.0)
    apply_fill(state, fill)
    # add_2 (6 shares) fully consumed; add_1 + base untouched
    assert len(state.addon_lots) == 1
    assert state.addon_lots[0].lot_id == "add_1"
    assert state.base_lot.shares == 10
    assert state.current_position_shares == 14
    assert state.realized_pnl == 60.0


def test_apply_fill_avg_entry_recalculated_after_every_fill():
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 100.0, 10),
        addon_lots=[make_lot("add_1", 110.0, 5)],
        avg_entry_price=103.33,
        current_position_shares=15,
    )
    fill = _fill(ActionType.SELL_TAKE_PROFIT, shares=5,
                 fill_price=130.0, realized_pnl=100.0)
    apply_fill(state, fill)
    # Only base remains: avg = 100
    assert state.avg_entry_price == 100.0
    assert state.current_position_shares == 10
    assert state.addon_lots == []


def test_apply_fill_sell_drops_base_lot_when_shares_reach_zero():
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 10),
        avg_entry_price=100.0,
        current_position_shares=10,
    )
    fill = _fill(ActionType.EXIT_ALL, shares=10,
                 fill_price=120.0, realized_pnl=200.0)
    apply_fill(state, fill)
    assert state.base_lot is None
    assert state.current_position_shares == 0
    assert state.avg_entry_price is None
    assert state.realized_pnl == 200.0


# ── state.json round-trip ───────────────────────────────────────────────────

def test_state_json_roundtrip(tmp_path):
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 900.0, 22),
        addon_lots=[make_lot("add_1", 945.0, 8)],
        avg_entry_price=924.06,
        current_position_shares=30,
        add_count=1,
        last_add_price=945.0,
        highest_price_since_entry=1000.0,
        trailing_stop_price=900.0,
        protect_profit_mode=False,
        target_price_tp_triggered=False,
    )
    p = tmp_path / "state.json"
    write_state(state, str(p))
    loaded = read_state(str(p))
    assert loaded.state == BotState.PYRAMID_LONG
    assert loaded.current_position_shares == 30
    assert loaded.base_lot.shares == 22
    assert loaded.addon_lots[0].lot_id == "add_1"


# ── reset_state ──────────────────────────────────────────────────────────────

def test_reset_state_to_flat_clears_all_runtime_fields():
    state = make_state(
        state=BotState.HALTED,
        base_lot=make_lot("base", 100.0, 10),
        addon_lots=[make_lot("add_1", 110.0, 5)],
        avg_entry_price=103.33,
        current_position_shares=15,
        add_count=1,
        protect_profit_mode=True,
        realized_pnl=-5000.0,
        halted=True,
        halt_reason="max_thesis_loss",
        target_price_tp_triggered=True,
        runner_mode_active=True,
        runner_target_shares=7,
    )
    reset_state_to_flat(state)
    assert state.state == BotState.FLAT
    assert state.base_lot is None
    assert state.addon_lots == []
    assert state.current_position_shares == 0
    assert state.add_count == 0
    assert state.protect_profit_mode is False
    assert state.realized_pnl == 0.0
    assert state.halted is False
    assert state.halt_reason is None
    assert state.target_price_tp_triggered is False
    assert state.runner_mode_active is False
    assert state.runner_target_shares is None
    assert state.tp_levels_triggered == [False, False, False, False]


def test_reset_state_script_refuses_without_confirm(tmp_path):
    """Section 23.13 — reset_state.py must reject runs without --confirm."""
    project_root = Path(__file__).resolve().parents[1]
    script = project_root / "scripts" / "reset_state.py"
    cfg_path = project_root / "config" / "mu_long_thesis.yaml"
    result = subprocess.run(
        [sys.executable, str(script), "--config", str(cfg_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "confirm" in result.stderr.lower()


# ── Cycle logging ────────────────────────────────────────────────────────────

def test_log_cycle_writes_all_four_csvs_including_no_action(tmp_path):
    cfg = base_config()
    state = make_state()
    decision = Decision(
        action=ActionType.NO_ACTION, shares=0, reason="no_trigger",
        blockers=["close_below_ma20"],
    )
    ind = make_indicators(adj_close=94.0, ma20=95.0)
    log_cycle(
        run_dir=str(tmp_path),
        state=state, decision=decision, fill=None,
        indicators=ind, equity=100_000,
        state_before="FLAT", state_after="FLAT",
        config_hash="a" * 64,
    )
    for fname in ("orders.csv", "positions.csv", "risk.csv", "decisions.csv"):
        assert (tmp_path / fname).exists()
    # decisions.csv must include the no_action row and the blocker
    dec_text = (tmp_path / "decisions.csv").read_text()
    assert "no_action" in dec_text
    assert "close_below_ma20" in dec_text


# ── Daily summary ────────────────────────────────────────────────────────────

def test_daily_summary_includes_required_fields():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 900.0, 22),
        addon_lots=[make_lot("add_1", 945.0, 8)],
        avg_entry_price=924.06,
        current_position_shares=30,
        add_count=2,
        highest_price_since_entry=1100.0,
        trailing_stop_price=990.0,
        initial_stop_price=775.0,
        days_to_event=15,
    )
    ind = make_indicators(adj_close=1000.0)
    summary = format_summary(state, ind, cfg,
                             action_label="NO ACTION",
                             reason="Close too extended above MA10")
    assert "PYRAMID_LONG" in summary
    assert "Avg Entry:" in summary
    assert "Trailing Stop:" in summary
    assert "Days to Event:       15" in summary
