from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

from scripts import run_live_hl as live_script
from src.core.config_loader import load_config
from src.core.models import (
    ActionType,
    BotState,
    Decision,
    LotRecord,
    OrderResult,
    OrderResultStatus,
    PendingOrder,
    ThesisState,
)
from src.hl.account import HLAccountSnapshot, HLOpenOrder, HLPosition
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
        date = __import__("datetime").date(2026, 5, 31)
        adj_close = 72317.0
        close = 72317.0
        ma5 = ma10 = ma20 = ma50 = atr14 = 50000.0
        prior_highest_high_20d = 50000.0
        avg_volume_20d = 1000.0
        open = high = low = 50000.0
        volume = 1000.0
        prev_adj_close = 50000.0
        intraday_return = 0.0
        gap_up_pct = gap_down_pct = 0.0
        distance_from_ma10 = distance_from_ma20 = 0.0
        drawdown_from_20d_high = 0.0
        bar_time = None
    return _Ind()


def _snapshot(
    *,
    position_qty: float = 0.0,
    open_buy_order: HLOpenOrder | None = None,
) -> HLAccountSnapshot:
    positions = []
    if position_qty > 0:
        positions = [
            HLPosition(
                coin="BTC",
                qty=position_qty,
                entry_price=72389.0,
                mark_price=72317.0,
                unrealized_pnl=0.0,
                liquidation_price=None,
                margin_used=100.0,
                leverage=1.0,
            )
        ]
    open_orders = [open_buy_order] if open_buy_order is not None else []
    return HLAccountSnapshot(
        account_value=0.0,
        margin_used=0.0,
        withdrawable=0.0,
        positions=positions,
        perp_account_value=0.0,
        perp_margin_used=0.0,
        perp_withdrawable=0.0,
        perp_positions=positions,
        spot_usdc_total=972.84,
        spot_usdc_hold=0.0,
        spot_usdc_available=972.84,
        open_orders_count=len(open_orders),
        open_orders=open_orders,
        collateral_mode="unified",
        effective_trading_collateral=972.84,
        trading_collateral_available=True,
        collateral_reason="unified_spot_usdc_available",
        perp_collateral_available=False,
    )


def _patch_common(monkeypatch, snapshot, decision):
    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *a, **kw: pd.DataFrame({"close": [72317.0]}))
    monkeypatch.setattr(live_script, "calc_indicators", lambda *a, **kw: _indicators())
    monkeypatch.setattr(live_script, "get_account_snapshot", lambda *a, **kw: snapshot)
    monkeypatch.setattr(live_script, "engine_run", lambda *a, **kw: decision)
    monkeypatch.setattr(live_script, "apply_candle_close_funding", lambda state, *a, **kw: state)
    monkeypatch.setattr(live_script, "format_summary", lambda *a, **kw: "")


def test_unfilled_opening_order_does_not_transition_state(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC")
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.00124,
        reason="starter_entry_conditions_met",
        new_state=BotState.STARTER_LONG,
    )
    _patch_common(monkeypatch, _snapshot(), decision)
    monkeypatch.setattr(
        live_script,
        "_dispatch_broker",
        lambda *a, **kw: OrderResult(
            status=OrderResultStatus.SUBMITTED_UNFILLED,
            action=ActionType.BUY_STARTER,
            oid=54015974761,
            side="buy",
            reduce_only=False,
            submitted_qty=0.00124,
            filled_qty=0.0,
            remaining_qty=0.00124,
            limit_px=72389.0,
            raw_status_sanitized="resting",
            state_before=BotState.FLAT.value,
            intended_state_after=BotState.STARTER_LONG.value,
            applied_state_after=BotState.FLAT.value,
            created_at=datetime.now(timezone.utc),
        ),
    )

    live_script._STOP_EVENT.clear()
    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )

    assert status == live_script.CONTINUE
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.FLAT
    assert persisted.current_position_qty == 0.0
    assert persisted.base_lot is None
    assert persisted.original_base_qty == 0.0
    assert persisted.pending_order is not None
    assert persisted.pending_order.oid == 54015974761
    orders_csv = (tmp_path / "orders.csv").read_text()
    decisions_csv = (tmp_path / "decisions.csv").read_text()
    assert "submitted_unfilled" in orders_csv
    assert "STARTER_LONG" in orders_csv
    assert "FLAT" in orders_csv
    assert "STARTER_LONG" in decisions_csv
    assert "FLAT" in decisions_csv
    assert ",FLAT," in (tmp_path / "positions.csv").read_text()
    assert "STARTER_LONG,0.0" not in (tmp_path / "positions.csv").read_text()


def test_filled_opening_order_transitions_state(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC")
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.00124,
        reason="starter_entry_conditions_met",
        new_state=BotState.STARTER_LONG,
    )
    _patch_common(monkeypatch, _snapshot(), decision)
    monkeypatch.setattr(
        live_script,
        "_dispatch_broker",
        lambda *a, **kw: OrderResult(
            status=OrderResultStatus.FILLED,
            action=ActionType.BUY_STARTER,
            side="buy",
            reduce_only=False,
            submitted_qty=0.00124,
            filled_qty=0.00124,
            remaining_qty=0.0,
            avg_fill_px=72389.0,
            limit_px=72389.0,
            state_before=BotState.FLAT.value,
            intended_state_after=BotState.STARTER_LONG.value,
            applied_state_after=BotState.STARTER_LONG.value,
            created_at=datetime.now(timezone.utc),
        ),
    )

    live_script._STOP_EVENT.clear()
    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )

    assert status == live_script.CONTINUE
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.STARTER_LONG
    assert persisted.base_lot is not None
    assert persisted.base_lot.qty > 0
    assert persisted.original_base_qty > 0


def test_partial_fill_transitions_by_filled_qty_only(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC")
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.00124,
        reason="starter_entry_conditions_met",
        new_state=BotState.STARTER_LONG,
    )
    _patch_common(monkeypatch, _snapshot(), decision)
    monkeypatch.setattr(
        live_script,
        "_dispatch_broker",
        lambda *a, **kw: OrderResult(
            status=OrderResultStatus.PARTIALLY_FILLED,
            action=ActionType.BUY_STARTER,
            side="buy",
            reduce_only=False,
            submitted_qty=0.00124,
            filled_qty=0.0005,
            remaining_qty=0.00074,
            avg_fill_px=72389.0,
            limit_px=72389.0,
            state_before=BotState.FLAT.value,
            intended_state_after=BotState.STARTER_LONG.value,
            applied_state_after=BotState.STARTER_LONG.value,
            created_at=datetime.now(timezone.utc),
        ),
    )

    live_script._STOP_EVENT.clear()
    live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )

    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.STARTER_LONG
    assert persisted.current_position_qty == 0.0005
    assert persisted.base_lot is not None
    assert persisted.base_lot.qty == 0.0005


def test_pending_order_blocks_duplicate_opening(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC", pending_order=PendingOrder(
        oid=54015974761,
        symbol="BTC",
        action="buy_starter",
        side="buy",
        reduce_only=False,
        order_type="limit",
        qty=0.00124,
        qty_submitted=0.00124,
        qty_filled=0.0,
        qty_remaining=0.00124,
        limit_px=72389.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
    ))
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.00124,
        reason="starter_entry_conditions_met",
        new_state=BotState.STARTER_LONG,
    )
    snapshot = _snapshot(open_buy_order=HLOpenOrder(
        oid=54015974761, coin="BTC", side="B", qty=0.00124, limit_px=72389.0
    ))
    _patch_common(monkeypatch, snapshot, decision)
    dispatch = MagicMock()
    monkeypatch.setattr(live_script, "_dispatch_broker", dispatch)

    live_script._STOP_EVENT.clear()
    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )

    assert status == live_script.BLOCKED_EXISTING_OPEN_ORDER
    dispatch.assert_not_called()
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.FLAT
    assert persisted.pending_order is not None
    assert persisted.pending_order.oid == 54015974761
    assert "existing_open_order" in (tmp_path / "decisions.csv").read_text()


def test_stale_pending_order_halts_for_cancel_or_reconcile(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.execution["pending_order_max_age_minutes"] = 10
    state = ThesisState(symbol="BTC", pending_order=PendingOrder(
        oid=54015974761,
        symbol="BTC",
        action="buy_starter",
        side="buy",
        reduce_only=False,
        order_type="limit",
        qty=0.00124,
        qty_submitted=0.00124,
        qty_filled=0.0,
        qty_remaining=0.00124,
        limit_px=72389.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
    ))
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.00124,
        reason="starter_entry_conditions_met",
        new_state=BotState.STARTER_LONG,
    )
    snapshot = _snapshot(open_buy_order=HLOpenOrder(
        oid=54015974761, coin="BTC", side="B", qty=0.00124, limit_px=72389.0
    ))
    _patch_common(monkeypatch, snapshot, decision)
    dispatch = MagicMock()
    monkeypatch.setattr(live_script, "_dispatch_broker", dispatch)

    live_script._STOP_EVENT.clear()
    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )

    assert status == live_script.STALE_PENDING_ORDER
    dispatch.assert_not_called()
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.HALTED
    assert persisted.halt_reason == "stale_pending_order_requires_cancel_or_reconcile"
    assert persisted.pending_order is not None
    assert persisted.pending_order.status == "stale"


def test_fresh_pending_order_stays_blocked_without_stale_halt(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.execution["pending_order_max_age_minutes"] = 10
    state = ThesisState(symbol="BTC", pending_order=PendingOrder(
        oid=54015974761,
        symbol="BTC",
        action="buy_starter",
        side="buy",
        reduce_only=False,
        order_type="limit",
        qty=0.00124,
        qty_submitted=0.00124,
        qty_filled=0.0,
        qty_remaining=0.00124,
        limit_px=72389.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    ))
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.00124,
        reason="starter_entry_conditions_met",
        new_state=BotState.STARTER_LONG,
    )
    snapshot = _snapshot(open_buy_order=HLOpenOrder(
        oid=54015974761, coin="BTC", side="B", qty=0.00124, limit_px=72389.0
    ))
    _patch_common(monkeypatch, snapshot, decision)
    dispatch = MagicMock()
    monkeypatch.setattr(live_script, "_dispatch_broker", dispatch)

    live_script._STOP_EVENT.clear()
    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )

    assert status == live_script.BLOCKED_EXISTING_OPEN_ORDER
    dispatch.assert_not_called()
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.FLAT
    assert persisted.pending_order is not None
    assert persisted.pending_order.status == "submitted_unfilled"


def test_exchange_position_without_reconstructable_fill_halts(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC")
    decision = Decision(action=ActionType.NO_ACTION, qty=0.0, reason="none")
    _patch_common(monkeypatch, _snapshot(position_qty=0.00124), decision)

    live_script._STOP_EVENT.clear()
    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )

    assert status == live_script.HALTED
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.HALTED
    assert persisted.halt_reason == "position_without_reconstructable_fill"


def test_submitted_unfilled_reduce_does_not_reduce_lots(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC", state=BotState.BASE_LONG)
    state.base_lot = LotRecord(
        lot_id="base",
        entry_price=70000.0,
        qty=0.00124,
        entry_date=date(2026, 5, 31),
    )
    state.current_position_qty = 0.00124
    state.avg_entry_price = 70000.0
    decision = Decision(
        action=ActionType.SELL_TAKE_PROFIT,
        qty=0.00124,
        reason="tp",
        new_state=BotState.EXITED,
    )
    _patch_common(monkeypatch, _snapshot(position_qty=0.00124), decision)
    monkeypatch.setattr(
        live_script,
        "_dispatch_broker",
        lambda *a, **kw: OrderResult(
            status=OrderResultStatus.SUBMITTED_UNFILLED,
            action=ActionType.SELL_TAKE_PROFIT,
            side="sell",
            reduce_only=True,
            submitted_qty=0.00124,
            filled_qty=0.0,
            remaining_qty=0.00124,
            limit_px=73000.0,
            state_before=BotState.BASE_LONG.value,
            intended_state_after=BotState.EXITED.value,
            applied_state_after=BotState.BASE_LONG.value,
            created_at=datetime.now(timezone.utc),
        ),
    )

    live_script._STOP_EVENT.clear()
    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )
    assert status == live_script.CONTINUE
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.BASE_LONG
    assert persisted.current_position_qty == 0.00124
    assert persisted.base_lot is not None and persisted.base_lot.qty == 0.00124


def test_partial_reduce_reduces_only_filled_qty(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC", state=BotState.BASE_LONG)
    state.base_lot = LotRecord(
        lot_id="base",
        entry_price=70000.0,
        qty=0.00124,
        entry_date=date(2026, 5, 31),
    )
    state.current_position_qty = 0.00124
    state.avg_entry_price = 70000.0
    decision = Decision(
        action=ActionType.SELL_TAKE_PROFIT,
        qty=0.00124,
        reason="tp",
        new_state=BotState.EXITED,
    )
    _patch_common(monkeypatch, _snapshot(position_qty=0.00124), decision)
    monkeypatch.setattr(
        live_script,
        "_dispatch_broker",
        lambda *a, **kw: OrderResult(
            status=OrderResultStatus.PARTIALLY_FILLED,
            action=ActionType.SELL_TAKE_PROFIT,
            side="sell",
            reduce_only=True,
            submitted_qty=0.00124,
            filled_qty=0.0005,
            remaining_qty=0.00074,
            avg_fill_px=73000.0,
            limit_px=73000.0,
            state_before=BotState.BASE_LONG.value,
            intended_state_after=BotState.EXITED.value,
            applied_state_after=BotState.EXITED.value,
            created_at=datetime.now(timezone.utc),
        ),
    )

    live_script._STOP_EVENT.clear()
    live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.BASE_LONG
    assert persisted.current_position_qty == 0.00074


def test_local_pending_missing_on_exchange_halts(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC", pending_order=PendingOrder(
        oid=54015974761,
        symbol="BTC",
        action="buy_starter",
        side="buy",
        reduce_only=False,
        order_type="limit",
        qty=0.00124,
        qty_submitted=0.00124,
        qty_filled=0.0,
        qty_remaining=0.00124,
        limit_px=72389.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
    ))
    decision = Decision(action=ActionType.NO_ACTION, qty=0.0, reason="none")
    _patch_common(monkeypatch, _snapshot(), decision)

    live_script._STOP_EVENT.clear()
    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )

    assert status == live_script.HALTED
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.halt_reason == "pending_order_missing_on_exchange"


def test_multiple_open_orders_halt(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC")
    decision = Decision(action=ActionType.NO_ACTION, qty=0.0, reason="none")
    snapshot = _snapshot(open_buy_order=HLOpenOrder(
        oid=1, coin="BTC", side="B", qty=0.00124, limit_px=72389.0
    ))
    snapshot.open_orders.append(HLOpenOrder(
        oid=2, coin="BTC", side="B", qty=0.00124, limit_px=72380.0
    ))
    snapshot.open_orders_count = 2
    _patch_common(monkeypatch, snapshot, decision)

    live_script._STOP_EVENT.clear()
    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )

    assert status == live_script.HALTED
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.halt_reason == "multiple_open_orders_not_supported"


def test_exchange_short_position_halts(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC")
    decision = Decision(action=ActionType.NO_ACTION, qty=0.0, reason="none")
    snapshot = _snapshot()
    snapshot.positions = [
        HLPosition(
            coin="BTC",
            qty=-0.001,
            entry_price=72389.0,
            mark_price=72317.0,
            unrealized_pnl=0.0,
            liquidation_price=None,
            margin_used=50.0,
            leverage=1.0,
        )
    ]
    _patch_common(monkeypatch, snapshot, decision)

    live_script._STOP_EVENT.clear()
    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )

    assert status == live_script.HALTED
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.halt_reason == "unexpected_short_position"


def test_unknown_order_result_halts(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC")
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.00124,
        reason="starter_entry_conditions_met",
        new_state=BotState.STARTER_LONG,
    )
    _patch_common(monkeypatch, _snapshot(), decision)
    monkeypatch.setattr(
        live_script,
        "_dispatch_broker",
        lambda *a, **kw: OrderResult(
            status=OrderResultStatus.UNKNOWN,
            action=ActionType.BUY_STARTER,
            side="buy",
            reduce_only=False,
            submitted_qty=0.00124,
            filled_qty=0.0,
            remaining_qty=0.00124,
            raw_status_sanitized="unknown",
            state_before=BotState.FLAT.value,
            intended_state_after=BotState.STARTER_LONG.value,
            applied_state_after=BotState.FLAT.value,
            created_at=datetime.now(timezone.utc),
        ),
    )

    live_script._STOP_EVENT.clear()
    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )

    assert status == live_script.HALTED
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.halt_reason == "unknown_order_result"
