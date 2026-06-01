from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

from scripts import probe_hl_add_order
from src.core.config_loader import load_config
from src.core.models import (
    ActionType,
    BotState,
    LotRecord,
    OrderResult,
    OrderResultStatus,
    ReconciliationResult,
    ReconciliationStatus,
    ThesisState,
)
from src.execution.reconciler import reconcile_state_with_exchange
from src.hl.account import HLAccountSnapshot, HLPosition
from src.reporting.state_writer import read_state, write_state
from src.strategy.add import check_add_conditions
from src.core.decision_engine import run
from tests._helpers import base_config, make_indicators, make_lot, make_state


def _runner_add_ready_state(**overrides) -> ThesisState:
    state = make_state(
        state=BotState.RUNNER_LONG,
        base_lot=make_lot("base", entry_price=900.0, qty=10, entry_date_str="2026-05-20"),
        avg_entry_price=900.0,
        current_position_qty=10,
        original_base_qty=20,
        entry_date=date(2026, 5, 20),
        last_add_price=900.0,
        add_count=0,
        trailing_stop_price=800.0,
    )
    state.runner_mode_active = True
    state.target_price_tp_triggered = True
    state.runner_target_qty = 10
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _ready_indicators(**overrides):
    values = {
        "date": date(2026, 5, 27),
        "adj_close": 946.0,
        "close": 946.0,
        "ma10": 920.0,
        "ma20": 910.0,
        "ma50": 890.0,
        "distance_from_ma10": 0.03,
        "distance_from_ma20": 0.04,
        "intraday_return": 0.01,
        "prior_highest_high_20d": 930.0,
    }
    values.update(overrides)
    return make_indicators(**values)


def test_runner_long_can_pass_add_eligibility():
    decision = check_add_conditions(_runner_add_ready_state(), _ready_indicators(), base_config())

    assert decision.action == ActionType.BUY_ADDON
    assert decision.new_state == BotState.PYRAMID_LONG


def test_runner_long_add_still_blocks_when_risk_budget_exhausted():
    decision = run(_runner_add_ready_state(trailing_stop_price=200.0), _ready_indicators(), base_config())

    assert decision.action == ActionType.NO_ACTION
    assert "thesis_risk_budget_exhausted" in decision.blockers


def test_runner_long_add_still_blocks_at_max_add_count():
    cfg = base_config()
    decision = check_add_conditions(
        _runner_add_ready_state(add_count=cfg.add["max_add_count"]),
        _ready_indicators(),
        cfg,
    )

    assert decision.action == ActionType.NO_ACTION
    assert "max_add_count_reached" in decision.blockers


def _probe_config(tmp_path: Path):
    cfg = load_config("config/btc_testnet_realistic_probe.yaml")
    cfg.logging["run_dir"] = str(tmp_path)
    cfg.notifications["enabled"] = False
    cfg.notifications["telegram_enabled"] = False
    return cfg


def _probe_runner_state() -> ThesisState:
    state = ThesisState(symbol="BTC", state=BotState.RUNNER_LONG)
    state.base_lot = LotRecord(
        lot_id="base",
        entry_price=72000.0,
        qty=0.005,
        entry_date=date(2026, 5, 20),
    )
    state.avg_entry_price = 72000.0
    state.current_position_qty = 0.005
    state.original_base_qty = 0.01
    state.runner_mode_active = True
    state.target_price_tp_triggered = True
    state.runner_target_qty = 0.005
    state.last_add_price = 72000.0
    state.sz_decimals = 5
    return state


def _snapshot(qty: float = 0.005) -> HLAccountSnapshot:
    position = HLPosition(
        coin="BTC",
        qty=qty,
        mark_price=72000.0,
        entry_price=72000.0,
        unrealized_pnl=0.0,
        liquidation_price=None,
        margin_used=10.0,
        leverage=1.0,
    )
    return HLAccountSnapshot(
        account_value=1000.0,
        margin_used=10.0,
        withdrawable=990.0,
        positions=[position],
        perp_positions=[position],
        open_orders_count=0,
        open_orders=[],
        effective_trading_collateral=1000.0,
        trading_collateral_available=True,
    )


def test_runner_long_probe_add_fill_creates_addon_and_enters_pyramid(tmp_path, monkeypatch):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0x1111111111111111111111111111111111111111")
    monkeypatch.setenv("HL_TESTNET_AGENT_PRIVATE_KEY", "0x" + "1" * 64)
    cfg = _probe_config(tmp_path)
    write_state(_probe_runner_state(), str(tmp_path / "state.json"))

    monkeypatch.setattr(probe_hl_add_order, "load_config", lambda _: cfg)
    monkeypatch.setattr(probe_hl_add_order, "get_private_key", lambda *_args, **_kwargs: "secret")
    monkeypatch.setattr(probe_hl_add_order, "HyperliquidClient", lambda network: object())
    monkeypatch.setattr(probe_hl_add_order, "get_account_snapshot", lambda *a, **kw: _snapshot())
    monkeypatch.setattr(probe_hl_add_order, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(
        probe_hl_add_order,
        "reconcile_state_with_exchange",
        lambda state, *a, **kw: (
            state,
            ReconciliationResult(
                status=ReconciliationStatus.OK,
                reason="ok",
                exchange_position_qty=state.current_position_qty,
                local_position_qty=state.current_position_qty,
            ),
        ),
    )

    def fake_execute(decision, state, *_args, **_kwargs):
        assert state.state == BotState.RUNNER_LONG
        return OrderResult(
            status=OrderResultStatus.FILLED,
            action=ActionType.BUY_ADDON,
            side="buy",
            reduce_only=False,
            submitted_qty=decision.qty,
            filled_qty=decision.qty,
            remaining_qty=0.0,
            avg_fill_px=72100.0,
            limit_px=72100.0,
            raw_status_sanitized="filled",
            state_before=BotState.RUNNER_LONG.value,
            intended_state_after=BotState.PYRAMID_LONG.value,
            applied_state_after=BotState.PYRAMID_LONG.value,
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(probe_hl_add_order, "execute_hl", fake_execute)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe_hl_add_order.py",
            "--config",
            "ignored.yaml",
            "--coin",
            "BTC",
            "--qty",
            "0.005",
            "--confirm",
            "--one-cycle",
        ],
    )

    assert probe_hl_add_order.main() == 0
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.PYRAMID_LONG
    assert persisted.base_lot is not None
    assert persisted.base_lot.qty == 0.005
    assert len(persisted.addon_lots) == 1
    assert persisted.addon_lots[0].qty == 0.005
    assert persisted.add_count == 1
