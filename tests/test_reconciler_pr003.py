from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from scripts import run_live_hl as live_script
from src.core.config_loader import load_config
from src.core.models import ActionType, BotState, LotRecord, PendingOrder, ReconciliationStatus, ThesisState
from src.execution.reconciler import reconcile_state_with_exchange
from src.hl.account import HLAccountSnapshot, HLFill, HLOpenOrder, HLPosition
from src.reporting.state_writer import read_state

TESTNET_WALLET = "0x1111111111111111111111111111111111111111"
FAKE_KEY = "0x" + "a" * 64


def _cfg(tmp_path: Path | None = None):
    cfg = load_config("config/mu_long_thesis.yaml")
    cfg.bot["mode"] = "testnet"
    cfg.data["source"] = "hyperliquid"
    if tmp_path is not None:
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


def _snapshot(
    *,
    position_qty: float = 0.0,
    open_orders: list[HLOpenOrder] | None = None,
    coin: str = "BTC",
) -> HLAccountSnapshot:
    positions = []
    if abs(position_qty) > 0:
        positions = [
            HLPosition(
                coin=coin,
                qty=position_qty,
                entry_price=72389.0,
                mark_price=72400.0,
                unrealized_pnl=0.0,
                liquidation_price=None,
                margin_used=30.0,
                leverage=1.0,
            )
        ]
    orders = open_orders or []
    return HLAccountSnapshot(
        account_value=30.0 if positions else 0.0,
        margin_used=0.0,
        withdrawable=0.0,
        positions=positions,
        perp_account_value=30.0 if positions else 0.0,
        perp_margin_used=0.0,
        perp_withdrawable=0.0,
        perp_positions=positions,
        spot_usdc_total=972.84,
        spot_usdc_hold=0.0,
        spot_usdc_available=972.84,
        open_orders_count=len(orders),
        open_orders=orders,
        collateral_mode="unified",
        effective_trading_collateral=972.84,
        trading_collateral_available=True,
        collateral_reason="unified_spot_usdc_available",
        perp_collateral_available=bool(positions),
    )


def _fill(qty: float, price: float, *, oid: int = 1, ts: int = 1_800_000_000_000) -> HLFill:
    return HLFill(
        coin="BTC",
        side="B",
        qty=qty,
        price=price,
        timestamp_ms=ts,
        oid=oid,
        direction="Open Long",
    )


def test_flat_no_position_no_open_orders_ok():
    state = ThesisState(symbol="BTC")
    state, result = reconcile_state_with_exchange(state, _snapshot(), [], _cfg())

    assert result.status == ReconciliationStatus.OK
    assert result.reason == "flat_in_sync"
    assert state.state == BotState.FLAT


def test_flat_open_buy_order_refreshes_pending_order():
    order = HLOpenOrder(oid=123, coin="BTC", side="B", qty=0.00124, limit_px=72389.0)
    state = ThesisState(symbol="BTC")

    state, result = reconcile_state_with_exchange(
        state, _snapshot(open_orders=[order]), [], _cfg()
    )

    assert result.status == ReconciliationStatus.REFRESHED_PENDING_ORDER
    assert state.state == BotState.FLAT
    assert state.pending_order is not None
    assert state.pending_order.oid == 123


def test_flat_exchange_position_exact_recent_fill_reconstructs_starter_long():
    state = ThesisState(symbol="BTC")

    state, result = reconcile_state_with_exchange(
        state,
        _snapshot(position_qty=0.00124),
        [_fill(0.00124, 72389.0)],
        _cfg(),
    )

    assert result.status == ReconciliationStatus.RECONSTRUCTED_POSITION
    assert state.state == BotState.STARTER_LONG
    assert state.current_position_qty == pytest.approx(0.00124)
    assert state.base_lot is not None
    assert state.base_lot.qty == pytest.approx(0.00124)
    assert state.original_base_qty == pytest.approx(0.00124)
    assert state.avg_entry_price == pytest.approx(72389.0)


def test_reconstructed_avg_entry_is_qty_weighted():
    state = ThesisState(symbol="BTC")

    state, result = reconcile_state_with_exchange(
        state,
        _snapshot(position_qty=0.003),
        [_fill(0.001, 70000.0, oid=1, ts=3), _fill(0.002, 73000.0, oid=2, ts=2)],
        _cfg(),
    )

    assert result.status == ReconciliationStatus.RECONSTRUCTED_POSITION
    assert state.avg_entry_price == pytest.approx((0.001 * 70000.0 + 0.002 * 73000.0) / 0.003)


def test_flat_exchange_position_no_fills_halts():
    state = ThesisState(symbol="BTC")
    state, result = reconcile_state_with_exchange(
        state, _snapshot(position_qty=0.00124), [], _cfg()
    )

    assert result.status == ReconciliationStatus.HALTED
    assert state.halt_reason == "position_without_reconstructable_fill"


def test_flat_exchange_position_fill_qty_mismatch_halts():
    state = ThesisState(symbol="BTC")
    state, result = reconcile_state_with_exchange(
        state, _snapshot(position_qty=0.00124), [_fill(0.001, 72389.0)], _cfg()
    )

    assert result.status == ReconciliationStatus.HALTED
    assert state.halt_reason == "position_without_reconstructable_fill"


def test_multiple_possible_fill_sets_halt():
    state = ThesisState(symbol="BTC")
    fills = [
        _fill(0.00124, 72389.0, oid=1, ts=3),
        _fill(0.0005, 72000.0, oid=2, ts=2),
        _fill(0.00074, 72100.0, oid=3, ts=1),
    ]
    state, result = reconcile_state_with_exchange(
        state, _snapshot(position_qty=0.00124), fills, _cfg()
    )

    assert result.status == ReconciliationStatus.HALTED
    assert state.halt_reason == "position_without_reconstructable_fill"


def test_pending_open_order_refreshes_pending():
    pending = PendingOrder(
        oid=123,
        symbol="BTC",
        action="buy_starter",
        side="buy",
        qty=0.00124,
        qty_submitted=0.00124,
        qty_remaining=0.00124,
        limit_px=72389.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
    )
    state = ThesisState(symbol="BTC", pending_order=pending)
    order = HLOpenOrder(oid=123, coin="BTC", side="B", qty=0.001, limit_px=72390.0)

    state, result = reconcile_state_with_exchange(
        state, _snapshot(open_orders=[order]), [], _cfg()
    )

    assert result.status == ReconciliationStatus.REFRESHED_PENDING_ORDER
    assert state.pending_order is not None
    assert state.pending_order.qty_remaining == pytest.approx(0.001)
    assert state.pending_order.limit_px == pytest.approx(72390.0)


def test_active_position_untracked_exchange_sell_order_recovers_pending_exit():
    state = ThesisState(symbol="BTC", state=BotState.STARTER_LONG)
    state.base_lot = LotRecord(
        lot_id="base",
        entry_price=72389.0,
        qty=0.00124,
        entry_date=date(2026, 5, 31),
    )
    state.current_position_qty = 0.00124
    state.original_base_qty = 0.00124
    state.avg_entry_price = 72389.0
    order = HLOpenOrder(oid=456, coin="BTC", side="A", qty=0.00124, limit_px=72000.0)

    state, result = reconcile_state_with_exchange(
        state, _snapshot(position_qty=0.00124, open_orders=[order]), [], _cfg()
    )

    assert result.status == ReconciliationStatus.REFRESHED_PENDING_ORDER
    assert result.reason == "recovered_untracked_open_order"
    assert state.state == BotState.STARTER_LONG
    assert state.current_position_qty == pytest.approx(0.00124)
    assert state.pending_order is not None
    assert state.pending_order.oid == 456
    assert state.pending_order.action == "exit_all"
    assert state.pending_order.side == "sell"
    assert state.pending_order.reduce_only is True


def test_active_position_untracked_exchange_buy_order_recovers_pending_add():
    state = ThesisState(symbol="BTC", state=BotState.BASE_LONG)
    state.base_lot = LotRecord(
        lot_id="base",
        entry_price=72389.0,
        qty=0.00124,
        entry_date=date(2026, 5, 31),
    )
    state.current_position_qty = 0.00124
    state.original_base_qty = 0.00124
    state.avg_entry_price = 72389.0
    order = HLOpenOrder(oid=789, coin="BTC", side="B", qty=0.001, limit_px=72450.0)

    state, result = reconcile_state_with_exchange(
        state, _snapshot(position_qty=0.00124, open_orders=[order]), [], _cfg()
    )

    assert result.status == ReconciliationStatus.REFRESHED_PENDING_ORDER
    assert result.reason == "recovered_untracked_open_order"
    assert state.state == BotState.BASE_LONG
    assert state.pending_order is not None
    assert state.pending_order.oid == 789
    assert state.pending_order.action == "buy_addon"
    assert state.pending_order.side == "buy"
    assert state.pending_order.reduce_only is False


def test_pending_missing_on_exchange_matching_fill_reconstructs():
    pending = PendingOrder(
        oid=123,
        symbol="BTC",
        action="buy_starter",
        side="buy",
        qty=0.00124,
        qty_submitted=0.00124,
        qty_remaining=0.00124,
        limit_px=72389.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
    )
    state = ThesisState(symbol="BTC", pending_order=pending)

    state, result = reconcile_state_with_exchange(
        state, _snapshot(position_qty=0.00124), [_fill(0.00124, 72389.0, oid=123)], _cfg()
    )

    assert result.status == ReconciliationStatus.RECONSTRUCTED_POSITION
    assert state.pending_order is None
    assert state.current_position_qty == pytest.approx(0.00124)


def test_pending_missing_on_exchange_no_fill_halts():
    pending = PendingOrder(
        oid=123,
        symbol="BTC",
        action="buy_starter",
        side="buy",
        qty=0.00124,
        qty_submitted=0.00124,
        qty_remaining=0.00124,
        limit_px=72389.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
    )
    state = ThesisState(symbol="BTC", pending_order=pending)

    state, result = reconcile_state_with_exchange(state, _snapshot(), [], _cfg())

    assert result.status == ReconciliationStatus.HALTED
    assert state.halt_reason == "pending_order_missing_on_exchange"


def test_pending_add_missing_on_exchange_split_fills_apply_single_addon_lot():
    pending = PendingOrder(
        oid=54110259097,
        symbol="BTC",
        action=ActionType.BUY_ADDON.value,
        side="buy",
        qty=0.005,
        qty_remaining=0.005,
        limit_px=71245.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
        state_before=BotState.RUNNER_LONG.value,
        intended_state_after=BotState.PYRAMID_LONG.value,
    )
    state = ThesisState(
        symbol="BTC",
        state=BotState.RUNNER_LONG,
        pending_order=pending,
        halted=True,
        halt_reason="pending_entry_missing_on_exchange",
    )
    state.base_lot = LotRecord(
        lot_id="base",
        entry_price=71500.0,
        qty=0.005,
        entry_date=date(2026, 6, 1),
    )
    state.current_position_qty = 0.005
    state.original_base_qty = 0.005
    state.avg_entry_price = 71500.0
    state.add_count = 0

    fills = [
        _fill(0.00161, 71245.0, oid=54110259097, ts=1_800_000_000_001),
        _fill(0.00209, 71245.0, oid=54110259097, ts=1_800_000_000_002),
        _fill(0.00130, 71245.0, oid=54110259097, ts=1_800_000_000_003),
    ]
    state, result = reconcile_state_with_exchange(
        state,
        _snapshot(position_qty=0.010),
        fills,
        _cfg(),
    )

    assert result.status == ReconciliationStatus.CLEARED_PENDING_ORDER
    assert result.reason == "cleared_filled_opening_pending_order"
    assert result.matched_fill_count == 3
    assert state.state == BotState.PYRAMID_LONG
    assert state.halted is False
    assert state.halt_reason is None
    assert state.pending_order is None
    assert state.current_position_qty == pytest.approx(0.010)
    assert state.base_lot is not None
    assert state.base_lot.qty == pytest.approx(0.005)
    assert len(state.addon_lots) == 1
    assert state.addon_lots[0].qty == pytest.approx(0.005)
    assert state.addon_lots[0].entry_price == pytest.approx(71245.0)
    assert state.add_count == 1
    assert state.avg_entry_price == pytest.approx((0.005 * 71500.0 + 0.005 * 71245.0) / 0.010)


def test_local_long_exchange_same_qty_ok():
    state = ThesisState(symbol="BTC", state=BotState.STARTER_LONG)
    state.base_lot = LotRecord(lot_id="base", entry_price=70000.0, qty=0.00124, entry_date=date(2026, 5, 31))
    state.current_position_qty = 0.00124
    state.avg_entry_price = 70000.0

    state, result = reconcile_state_with_exchange(
        state, _snapshot(position_qty=0.00124), [], _cfg()
    )

    assert result.status == ReconciliationStatus.OK
    assert state.state == BotState.STARTER_LONG


@pytest.mark.parametrize("exchange_qty", [0.0, 0.002])
def test_local_long_exchange_mismatch_halts(exchange_qty):
    state = ThesisState(symbol="BTC", state=BotState.STARTER_LONG)
    state.base_lot = LotRecord(lot_id="base", entry_price=70000.0, qty=0.00124, entry_date=date(2026, 5, 31))
    state.current_position_qty = 0.00124
    state.avg_entry_price = 70000.0

    state, result = reconcile_state_with_exchange(
        state, _snapshot(position_qty=exchange_qty), [], _cfg()
    )

    assert result.status == ReconciliationStatus.HALTED
    assert state.halt_reason == "local_exchange_position_mismatch"


def test_multiple_open_orders_halt():
    state = ThesisState(symbol="BTC")
    orders = [
        HLOpenOrder(oid=1, coin="BTC", side="B", qty=0.001, limit_px=70000.0),
        HLOpenOrder(oid=2, coin="BTC", side="B", qty=0.001, limit_px=70100.0),
    ]
    state, result = reconcile_state_with_exchange(
        state, _snapshot(open_orders=orders), [], _cfg()
    )

    assert result.status == ReconciliationStatus.HALTED
    assert state.halt_reason == "multiple_open_orders_not_supported"


def test_short_position_halts():
    state = ThesisState(symbol="BTC")
    state, result = reconcile_state_with_exchange(
        state, _snapshot(position_qty=-0.001), [], _cfg()
    )

    assert result.status == ReconciliationStatus.HALTED
    assert state.halt_reason == "unexpected_short_position"


def test_no_reconstruction_from_limit_price_alone():
    pending = PendingOrder(
        oid=123,
        symbol="BTC",
        action="buy_starter",
        side="buy",
        qty=0.00124,
        qty_submitted=0.00124,
        qty_remaining=0.00124,
        limit_px=72389.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
    )
    state = ThesisState(symbol="BTC", pending_order=pending)

    state, result = reconcile_state_with_exchange(
        state, _snapshot(position_qty=0.00124), [], _cfg()
    )

    assert result.status == ReconciliationStatus.HALTED


def test_live_cycle_reconstructs_and_does_not_submit_new_order(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC", state=BotState.HALTED, halted=True, halt_reason="position_without_reconstructable_fill")
    decision_engine = MagicMock()
    dispatch = MagicMock()

    class _Ind:
        date = date(2026, 5, 31)
        adj_close = close = 72400.0
        ma5 = ma10 = ma20 = ma50 = atr14 = 50000.0
        prior_highest_high_20d = 50000.0
        avg_volume_20d = 1000.0
        open = high = low = 50000.0
        volume = 1000.0
        prev_adj_close = 50000.0
        intraday_return = gap_up_pct = gap_down_pct = 0.0
        distance_from_ma10 = distance_from_ma20 = drawdown_from_20d_high = 0.0
        bar_time = None

    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *a, **kw: pd.DataFrame({"close": [72400.0]}))
    monkeypatch.setattr(live_script, "calc_indicators", lambda *a, **kw: _Ind())
    monkeypatch.setattr(live_script, "get_account_snapshot", lambda *a, **kw: _snapshot(position_qty=0.00124))
    monkeypatch.setattr(live_script, "get_recent_user_fills", lambda *a, **kw: [_fill(0.00124, 72389.0)])
    monkeypatch.setattr(live_script, "engine_run", decision_engine)
    monkeypatch.setattr(live_script, "_dispatch_broker", dispatch)
    monkeypatch.setattr(live_script, "apply_candle_close_funding", lambda state, *a, **kw: state)
    monkeypatch.setattr(live_script, "format_summary", lambda *a, **kw: "")
    live_script._STOP_EVENT.clear()

    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json", MagicMock(), TESTNET_WALLET, FAKE_KEY
    )

    assert status == live_script.RECONCILED
    decision_engine.assert_not_called()
    dispatch.assert_not_called()
    persisted = read_state(str(tmp_path / "state.json"))
    assert persisted.state == BotState.STARTER_LONG
    assert persisted.current_position_qty == pytest.approx(0.00124)
    assert "reconstructed_from_recent_fills" in (tmp_path / "decisions.csv").read_text()
