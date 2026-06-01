from __future__ import annotations

import math
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from src.core.models import (
    ActionType,
    BotState,
    Decision,
    Fill,
    HLAssetMeta,
    LotRecord,
    ThesisState,
)
from src.strategy.reduce import lifo_reduce, recalc_avg_entry_price
from tests._helpers import make_lot, make_state


def _lot(lot_id: str, price: float, qty: float) -> LotRecord:
    return LotRecord(lot_id=lot_id, entry_price=price, qty=qty, entry_date=date(2026, 1, 1))


def test_lot_record_uses_qty_field():
    lot = _lot("base", 50_000.0, 0.005)
    assert math.isclose(lot.qty, 0.005)
    assert not hasattr(lot, "shares")


def test_lot_record_rejects_legacy_share_field():
    with pytest.raises(ValidationError):
        LotRecord(lot_id="base", entry_price=50_000.0, shares=1, entry_date=date(2026, 1, 1))


def test_thesis_state_qty_field_defaults_zero():
    state = ThesisState(symbol="BTC")
    assert state.current_position_qty == 0.0
    assert not hasattr(state, "current_position_shares")


def test_thesis_state_rejects_legacy_share_field():
    with pytest.raises(ValidationError):
        ThesisState(symbol="BTC", current_position_shares=1)


def test_thesis_state_runner_target_qty():
    state = ThesisState(symbol="BTC", runner_target_qty=0.25)
    assert math.isclose(state.runner_target_qty, 0.25)
    assert not hasattr(state, "runner_target_shares")


def test_hl_asset_meta_validates_correctly():
    meta = HLAssetMeta(coin="BTC", sz_decimals=3, min_size=0.001, max_leverage=50.0)
    assert meta.coin == "BTC"
    assert meta.sz_decimals == 3
    assert math.isclose(meta.min_size, 0.001)
    assert math.isclose(meta.max_leverage, 50.0)


def test_fill_uses_qty_field():
    fill = Fill(
        action=ActionType.BUY_BASE,
        qty=0.005,
        fill_price=50_000.0,
        slippage_bps=2.5,
        timestamp=datetime.now(),
    )
    assert math.isclose(fill.qty, 0.005)
    assert not hasattr(fill, "shares")


def test_decision_uses_qty_field():
    decision = Decision(action=ActionType.BUY_ADDON, qty=0.001, reason="test")
    assert math.isclose(decision.qty, 0.001)
    assert not hasattr(decision, "shares")


def test_lifo_reduce_float_lots_correct():
    lots = [_lot("add_1", 50_000.0, 0.010), _lot("add_2", 52_000.0, 0.005)]
    updated, sold = lifo_reduce(lots, qty_to_sell=0.005, sz_decimals=3)
    assert math.isclose(sold, 0.005)
    assert len(updated) == 1
    assert updated[0].lot_id == "add_1"
    assert math.isclose(updated[0].qty, 0.010)


def test_lifo_reduce_float_lots_partial():
    lots = [_lot("add_1", 50_000.0, 0.010), _lot("add_2", 52_000.0, 0.008)]
    updated, sold = lifo_reduce(lots, qty_to_sell=0.005, sz_decimals=3)
    assert math.isclose(sold, 0.005)
    assert len(updated) == 2
    assert updated[1].lot_id == "add_2"
    assert math.isclose(updated[1].qty, 0.003)


def test_lifo_reduce_removes_lot_below_min_size():
    lots = [_lot("add_1", 50_000.0, 0.010), _lot("add_2", 52_000.0, 0.0015)]
    updated, sold = lifo_reduce(lots, qty_to_sell=0.001, sz_decimals=4, min_size=0.001)
    assert len(updated) == 1
    assert updated[0].lot_id == "add_1"
    assert math.isclose(sold, 0.001)


def test_recalc_avg_entry_price_float_qty():
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=_lot("base", 50_000.0, 0.010),
        addon_lots=[_lot("add_1", 52_000.0, 0.005)],
    )
    recalc_avg_entry_price(state)
    expected_avg = (0.010 * 50_000.0 + 0.005 * 52_000.0) / 0.015
    assert math.isclose(state.avg_entry_price, expected_avg, rel_tol=1e-9)
    assert math.isclose(state.current_position_qty, 0.015, rel_tol=1e-9)


def test_recalc_avg_entry_price_integer_qty():
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 100.0, 10),
        addon_lots=[make_lot("add_1", 110.0, 5)],
    )
    recalc_avg_entry_price(state)
    expected_avg = (10 * 100.0 + 5 * 110.0) / 15
    assert math.isclose(state.avg_entry_price, expected_avg, rel_tol=1e-9)
    assert math.isclose(state.current_position_qty, 15.0)
