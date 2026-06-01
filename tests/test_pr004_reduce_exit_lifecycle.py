from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from scripts import run_live_hl as live_script
from src.core.config_loader import load_config
from src.core.models import (
    ActionClass,
    ActionType,
    BotState,
    Decision,
    LotRecord,
    OrderResult,
    OrderResultStatus,
    PendingOrder,
    ThesisState,
)
from src.execution.lifecycle import classify_action
from src.execution.reconciler import reconcile_state_with_exchange
from src.hl.account import HLAccountSnapshot, HLFill, HLOpenOrder, HLPosition
from src.reporting.state_writer import read_state

TESTNET_WALLET = "0x1111111111111111111111111111111111111111"
FAKE_KEY = "0x" + "a" * 64


def _cfg(tmp_path: Path):
    cfg = load_config("config/mu_long_thesis.yaml")
    cfg.bot["mode"] = "testnet"
    cfg.data["source"] = "hyperliquid"
    cfg.logging["run_dir"] = str(tmp_path)
    cfg.hl = {
        "network": "testnet",
        "coin": "BTC",
        "wallet_address": TESTNET_WALLET,
        "bar_interval": "4h",
        "sz_decimals": 5,
        "min_size": 0.001,
        "collateral_mode": "unified",
    }
    return cfg


def _indicators():
    class _Ind:
        date = date(2026, 5, 31)
        adj_close = close = 73000.0
        ma5 = ma10 = ma20 = ma50 = atr14 = 50000.0
        prior_highest_high_20d = 50000.0
        avg_volume_20d = 1000.0
        open = high = low = 50000.0
        volume = 1000.0
        prev_adj_close = 50000.0
        intraday_return = gap_up_pct = gap_down_pct = 0.0
        distance_from_ma10 = distance_from_ma20 = drawdown_from_20d_high = 0.0
        bar_time = None
    return _Ind()


def _state(qty: float = 0.00124, *, state: BotState = BotState.BASE_LONG) -> ThesisState:
    s = ThesisState(symbol="BTC", state=state)
    s.base_lot = LotRecord(
        lot_id="base",
        entry_price=70000.0,
        qty=qty,
        entry_date=date(2026, 5, 30),
    )
    s.current_position_qty = qty
    s.original_base_qty = qty
    s.avg_entry_price = 70000.0
    return s


def _snapshot(position_qty: float = 0.00124, open_orders: list[HLOpenOrder] | None = None):
    positions = []
    if abs(position_qty) > 0:
        positions = [
            HLPosition(
                coin="BTC",
                qty=position_qty,
                entry_price=70000.0,
                mark_price=73000.0,
                unrealized_pnl=0.0,
                liquidation_price=None,
                margin_used=30.0,
                leverage=1.0,
            )
        ]
    return HLAccountSnapshot(
        account_value=30.0,
        margin_used=0.0,
        withdrawable=0.0,
        positions=positions,
        perp_account_value=30.0,
        perp_margin_used=0.0,
        perp_withdrawable=0.0,
        perp_positions=positions,
        spot_usdc_total=900.0,
        spot_usdc_hold=0.0,
        spot_usdc_available=900.0,
        open_orders=open_orders or [],
        open_orders_count=len(open_orders or []),
        collateral_mode="unified",
        effective_trading_collateral=900.0,
        trading_collateral_available=True,
        collateral_reason="unified_spot_usdc_available",
        perp_collateral_available=True,
    )


def _patch_cycle(monkeypatch, snapshot, decision_or_fn, dispatch_result=None):
    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *a, **kw: pd.DataFrame({"close": [73000.0]}))
    monkeypatch.setattr(live_script, "calc_indicators", lambda *a, **kw: _indicators())
    monkeypatch.setattr(live_script, "get_account_snapshot", lambda *a, **kw: snapshot)
    monkeypatch.setattr(live_script, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(live_script, "engine_run", decision_or_fn if callable(decision_or_fn) else lambda *a, **kw: decision_or_fn)
    monkeypatch.setattr(live_script, "apply_candle_close_funding", lambda state, *a, **kw: state)
    monkeypatch.setattr(live_script, "format_summary", lambda *a, **kw: "")
    if dispatch_result is not None:
        monkeypatch.setattr(live_script, "_dispatch_broker", lambda *a, **kw: dispatch_result)


def _result(action, status, *, submitted=0.00124, filled=0.0, remaining=None, px=73000.0, side="sell"):
    if remaining is None:
        remaining = max(submitted - filled, 0.0)
    return OrderResult(
        status=status,
        oid=123,
        action=action,
        side=side,
        reduce_only=side == "sell",
        submitted_qty=submitted,
        filled_qty=filled,
        remaining_qty=remaining,
        avg_fill_px=px if filled > 0 else None,
        limit_px=px,
        state_before=BotState.BASE_LONG.value,
        intended_state_after=BotState.EXITED.value,
        applied_state_after=BotState.BASE_LONG.value,
        created_at=datetime.now(timezone.utc),
    )


def _run(tmp_path, cfg, state):
    live_script._STOP_EVENT.clear()
    return live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )


def test_action_classification():
    assert classify_action(ActionType.BUY_STARTER) == ActionClass.OPENING
    assert classify_action(ActionType.BUY_ADDON) == ActionClass.ADDING
    assert classify_action(ActionType.SELL_TAKE_PROFIT) == ActionClass.RISK_REDUCING
    assert classify_action(ActionType.SELL_EVENT_DERISKING) == ActionClass.RISK_REDUCING
    assert classify_action(ActionType.SELL_STOP) == ActionClass.EMERGENCY_EXIT
    assert classify_action(ActionType.EXIT_ALL) == ActionClass.EMERGENCY_EXIT
    assert classify_action(ActionType.NO_ACTION) == ActionClass.NON_ORDER


def test_reduce_submitted_unfilled_keeps_lots_and_records_pending(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state(state=BotState.PYRAMID_LONG)
    state.addon_lots = [LotRecord(lot_id="add_1", entry_price=71000.0, qty=0.0005, entry_date=date(2026, 5, 31))]
    state.current_position_qty = 0.00174
    decision = Decision(action=ActionType.SELL_REDUCE_ADDON, qty=0.0005, reason="reduce", new_state=BotState.REDUCE_MODE)
    _patch_cycle(monkeypatch, _snapshot(position_qty=0.00174), decision, _result(ActionType.SELL_REDUCE_ADDON, OrderResultStatus.SUBMITTED_UNFILLED, submitted=0.0005))

    _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.PYRAMID_LONG
    assert persisted.current_position_qty == pytest.approx(0.00174)
    assert len(persisted.addon_lots) == 1
    assert persisted.pending_order is not None
    assert "pending_reduce" in (tmp_path / "risk.csv").read_text()


def test_reduce_partial_fill_reduces_only_filled_qty(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state(state=BotState.PYRAMID_LONG)
    state.addon_lots = [LotRecord(lot_id="add_1", entry_price=71000.0, qty=0.0005, entry_date=date(2026, 5, 31))]
    state.current_position_qty = 0.00174
    decision = Decision(action=ActionType.SELL_REDUCE_ADDON, qty=0.0005, reason="reduce", new_state=BotState.REDUCE_MODE)
    _patch_cycle(monkeypatch, _snapshot(position_qty=0.00174), decision, _result(ActionType.SELL_REDUCE_ADDON, OrderResultStatus.PARTIALLY_FILLED, submitted=0.0005, filled=0.0002, remaining=0.0003))

    _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.current_position_qty == pytest.approx(0.00154)
    assert persisted.pending_order is not None
    assert persisted.pending_order.qty_remaining == pytest.approx(0.0003)


def test_reduce_full_fill_updates_lots(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state(state=BotState.PYRAMID_LONG)
    state.addon_lots = [LotRecord(lot_id="add_1", entry_price=71000.0, qty=0.0005, entry_date=date(2026, 5, 31))]
    state.current_position_qty = 0.00174
    decision = Decision(action=ActionType.SELL_REDUCE_ADDON, qty=0.0005, reason="reduce", new_state=BotState.REDUCE_MODE)
    _patch_cycle(monkeypatch, _snapshot(position_qty=0.00174), decision, _result(ActionType.SELL_REDUCE_ADDON, OrderResultStatus.FILLED, submitted=0.0005, filled=0.0005, remaining=0.0))

    _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.current_position_qty == pytest.approx(0.00124)
    assert persisted.addon_lots == []


def test_hard_stop_submitted_unfilled_halts_without_exit(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state()
    decision = Decision(action=ActionType.SELL_STOP, qty=0.00124, reason="hard_stop", new_state=BotState.EXITED)
    _patch_cycle(monkeypatch, _snapshot(), decision, _result(ActionType.SELL_STOP, OrderResultStatus.SUBMITTED_UNFILLED))

    status = _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert status == live_script.HALTED
    assert persisted.state == BotState.HALTED
    assert persisted.current_position_qty == pytest.approx(0.00124)
    assert persisted.halt_reason == "unfilled_emergency_exit"


def test_hard_stop_partial_fill_halts_with_residual(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state()
    decision = Decision(action=ActionType.SELL_STOP, qty=0.00124, reason="hard_stop", new_state=BotState.EXITED)
    _patch_cycle(monkeypatch, _snapshot(), decision, _result(ActionType.SELL_STOP, OrderResultStatus.PARTIALLY_FILLED, filled=0.0005, remaining=0.00074))

    status = _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert status == live_script.HALTED
    assert persisted.state == BotState.HALTED
    assert persisted.current_position_qty == pytest.approx(0.00074)
    assert persisted.halt_reason == "residual_position_after_emergency_exit"


def test_hard_stop_full_fill_exits(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state()
    decision = Decision(action=ActionType.SELL_STOP, qty=0.00124, reason="hard_stop", new_state=BotState.EXITED)
    _patch_cycle(monkeypatch, _snapshot(), decision, _result(ActionType.SELL_STOP, OrderResultStatus.FILLED, filled=0.00124, remaining=0.0))

    _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.EXITED
    assert persisted.current_position_qty == 0.0


def test_max_loss_full_fill_halts(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state()
    decision = Decision(action=ActionType.EXIT_ALL, qty=0.00124, reason="max_thesis_loss", new_state=BotState.HALTED)
    _patch_cycle(monkeypatch, _snapshot(), decision, _result(ActionType.EXIT_ALL, OrderResultStatus.FILLED, filled=0.00124, remaining=0.0))

    _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.HALTED
    assert persisted.halted is True
    assert persisted.current_position_qty == 0.0


def test_layered_tp_submitted_unfilled_does_not_mark_triggered(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state()

    def engine_run(state_for_engine, *a, **kw):
        state_for_engine.tp_levels_triggered[0] = True
        return Decision(action=ActionType.SELL_TAKE_PROFIT, qty=0.0005, reason="layered_tp_level_1")

    _patch_cycle(monkeypatch, _snapshot(), engine_run, _result(ActionType.SELL_TAKE_PROFIT, OrderResultStatus.SUBMITTED_UNFILLED, submitted=0.0005))
    _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.tp_levels_triggered[0] is False
    assert persisted.current_position_qty == pytest.approx(0.00124)


def test_layered_tp_full_fill_marks_triggered(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state()

    def engine_run(state_for_engine, *a, **kw):
        state_for_engine.tp_levels_triggered[0] = True
        return Decision(action=ActionType.SELL_TAKE_PROFIT, qty=0.0005, reason="layered_tp_level_1")

    _patch_cycle(monkeypatch, _snapshot(), engine_run, _result(ActionType.SELL_TAKE_PROFIT, OrderResultStatus.FILLED, submitted=0.0005, filled=0.0005, remaining=0.0))
    _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.tp_levels_triggered[0] is True
    assert persisted.current_position_qty == pytest.approx(0.00074)


def test_target_tp_unfilled_does_not_enter_runner(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state()

    def engine_run(state_for_engine, *a, **kw):
        state_for_engine.target_price_tp_triggered = True
        state_for_engine.runner_mode_active = True
        state_for_engine.runner_target_qty = 0.00031
        return Decision(action=ActionType.SELL_TAKE_PROFIT, qty=0.00093, reason="target_tp_runner_mode", new_state=BotState.RUNNER_LONG)

    _patch_cycle(monkeypatch, _snapshot(), engine_run, _result(ActionType.SELL_TAKE_PROFIT, OrderResultStatus.SUBMITTED_UNFILLED, submitted=0.00093))
    _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.BASE_LONG
    assert persisted.target_price_tp_triggered is False
    assert persisted.runner_mode_active is False


def test_target_tp_full_reduce_to_runner_enters_runner(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state()

    def engine_run(state_for_engine, *a, **kw):
        state_for_engine.target_price_tp_triggered = True
        state_for_engine.runner_mode_active = True
        state_for_engine.runner_target_qty = 0.00031
        return Decision(action=ActionType.SELL_TAKE_PROFIT, qty=0.00093, reason="target_tp_runner_mode", new_state=BotState.RUNNER_LONG)

    _patch_cycle(monkeypatch, _snapshot(), engine_run, _result(ActionType.SELL_TAKE_PROFIT, OrderResultStatus.FILLED, submitted=0.00093, filled=0.00093, remaining=0.0))
    _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.RUNNER_LONG
    assert persisted.current_position_qty == pytest.approx(0.00031)
    assert persisted.runner_target_qty == pytest.approx(0.00031)


def test_event_risk_reduce_submitted_unfilled_keeps_exposure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state(state=BotState.EVENT_RISK_MODE)
    decision = Decision(action=ActionType.SELL_EVENT_DERISKING, qty=0.0005, reason="event_t5_reduce")
    _patch_cycle(monkeypatch, _snapshot(), decision, _result(ActionType.SELL_EVENT_DERISKING, OrderResultStatus.SUBMITTED_UNFILLED, submitted=0.0005))

    _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.EVENT_RISK_MODE
    assert persisted.current_position_qty == pytest.approx(0.00124)
    assert persisted.pending_order is not None
    assert "pending_event_risk_reduce" in (tmp_path / "risk.csv").read_text()


def test_event_risk_force_flat_unfilled_halts(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state(state=BotState.EVENT_RISK_MODE)
    decision = Decision(action=ActionType.EXIT_ALL, qty=0.00124, reason="event_force_flat", new_state=BotState.EXITED)
    _patch_cycle(monkeypatch, _snapshot(), decision, _result(ActionType.EXIT_ALL, OrderResultStatus.SUBMITTED_UNFILLED))

    _run(tmp_path, cfg, state)
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.HALTED
    assert persisted.current_position_qty == pytest.approx(0.00124)


def test_pending_reduce_blocks_add(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state()
    state.pending_order = PendingOrder(
        oid=77,
        symbol="BTC",
        action=ActionType.SELL_TAKE_PROFIT.value,
        side="sell",
        reduce_only=True,
        qty=0.0005,
        qty_submitted=0.0005,
        qty_remaining=0.0005,
        limit_px=73000.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
    )
    order = HLOpenOrder(oid=77, coin="BTC", side="A", qty=0.0005, limit_px=73000.0)
    decision = Decision(action=ActionType.BUY_ADDON, qty=0.001, reason="add", new_state=BotState.PYRAMID_LONG)
    dispatch = MagicMock()
    _patch_cycle(monkeypatch, _snapshot(open_orders=[order]), decision)
    monkeypatch.setattr(live_script, "_dispatch_broker", dispatch)

    status = _run(tmp_path, cfg, state)
    assert status == live_script.BLOCKED_EXISTING_OPEN_ORDER
    dispatch.assert_not_called()


def test_pending_exit_blocks_new_order(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _state()
    state.pending_order = PendingOrder(
        oid=88,
        symbol="BTC",
        action=ActionType.EXIT_ALL.value,
        side="sell",
        reduce_only=True,
        qty=0.00124,
        qty_submitted=0.00124,
        qty_remaining=0.00124,
        limit_px=73000.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
    )
    order = HLOpenOrder(oid=88, coin="BTC", side="A", qty=0.00124, limit_px=73000.0)
    decision = Decision(action=ActionType.BUY_ADDON, qty=0.001, reason="add", new_state=BotState.PYRAMID_LONG)
    dispatch = MagicMock()
    _patch_cycle(monkeypatch, _snapshot(open_orders=[order]), decision)
    monkeypatch.setattr(live_script, "_dispatch_broker", dispatch)

    status = _run(tmp_path, cfg, state)
    assert status == live_script.BLOCKED_EXISTING_OPEN_ORDER
    dispatch.assert_not_called()


def test_reconciliation_applies_matching_sell_fill_for_pending_reduce():
    state = _state()
    state.pending_order = PendingOrder(
        oid=99,
        symbol="BTC",
        action=ActionType.SELL_TAKE_PROFIT.value,
        side="sell",
        reduce_only=True,
        qty=0.0005,
        qty_submitted=0.0005,
        qty_remaining=0.0005,
        limit_px=73000.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
    )
    fill = HLFill(coin="BTC", side="A", qty=0.0005, price=73000.0, timestamp_ms=1_800_000_000_000, oid=99, direction="Close Long")
    state, result = reconcile_state_with_exchange(state, _snapshot(position_qty=0.00074), [fill], _cfg(Path("/tmp")))
    assert result.reason == "reconciled_pending_reduce_from_recent_fills"
    assert state.current_position_qty == pytest.approx(0.00074)
    assert state.pending_order is None


def test_reconciliation_halts_when_pending_reduce_disappears_without_fill():
    state = _state()
    state.pending_order = PendingOrder(
        oid=99,
        symbol="BTC",
        action=ActionType.SELL_TAKE_PROFIT.value,
        side="sell",
        reduce_only=True,
        qty=0.0005,
        qty_submitted=0.0005,
        qty_remaining=0.0005,
        limit_px=73000.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
    )
    state, result = reconcile_state_with_exchange(state, _snapshot(position_qty=0.00074), [], _cfg(Path("/tmp")))
    assert result.status.value == "halted"
    assert state.halt_reason == "pending_reduce_missing_on_exchange"
