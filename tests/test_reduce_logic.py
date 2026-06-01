"""Section 22.6 — Reduce tests."""
from src.core.models import ActionType, BotState, LotRecord
from src.strategy.reduce import (
    check_addon_reduce, check_base_reduce, lifo_reduce, recalc_avg_entry_price,
)
from tests._helpers import base_config, make_indicators, make_lot, make_state


def _three_addons_state():
    return make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 900.0, 22),
        addon_lots=[
            make_lot("add_1", 945.0, 8, "2026-05-22"),
            make_lot("add_2", 1000.0, 6, "2026-05-25"),
            make_lot("add_3", 1050.0, 4, "2026-05-27"),
        ],
        avg_entry_price=924.06,
        current_position_qty=40,
        highest_price_since_entry=1100.0,
    )


def test_reduce_latest_addon_when_only_close_below_ma5():
    cfg = base_config()
    state = _three_addons_state()
    # close < ma5 but >= ma10; drawdown 1100→1080 = ~1.8% < 10%
    ind = make_indicators(adj_close=1080.0, ma5=1090, ma10=1070, ma20=1000, ma50=900)
    d = check_addon_reduce(state, ind, cfg)
    assert d.action == ActionType.SELL_REDUCE_ADDON
    assert d.qty == 4   # latest addon (add_3) only
    assert d.reason == "addon_reduce_latest_ma5"


def test_reduce_all_addons_when_close_below_ma10():
    cfg = base_config()
    state = _three_addons_state()
    ind = make_indicators(adj_close=1050.0, ma5=1080, ma10=1070, ma20=1000, ma50=900)
    d = check_addon_reduce(state, ind, cfg)
    assert d.action == ActionType.SELL_REDUCE_ADDON
    assert d.qty == 18   # all addons
    assert "ma10" in d.reason


def test_reduce_all_addons_not_doubled_when_ma5_and_ma10_both_broken():
    cfg = base_config()
    state = _three_addons_state()
    # close below BOTH ma5 and ma10 — must run exactly ONE reduce
    ind = make_indicators(adj_close=1000.0, ma5=1080, ma10=1070, ma20=900, ma50=800)
    d = check_addon_reduce(state, ind, cfg)
    # Rule B wins over Rule A — single "reduce all" action
    assert d.qty == 18


def test_reduce_all_addons_when_drawdown_from_peak_exceeds_threshold():
    cfg = base_config()
    state = _three_addons_state()
    # peak=1100; close=985 → drawdown = (1100-985)/1100 = 10.45% ≥ 10%
    ind = make_indicators(adj_close=985.0, ma5=970, ma10=970, ma20=950, ma50=850)
    d = check_addon_reduce(state, ind, cfg)
    assert d.action == ActionType.SELL_REDUCE_ADDON
    assert d.qty == 18


def test_reduce_base_half_when_close_below_ma20():
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 900.0, 22),
        avg_entry_price=900.0,
        current_position_qty=22,
        highest_price_since_entry=950.0,
    )
    ind = make_indicators(adj_close=920.0, ma5=930, ma10=930, ma20=930, ma50=850)
    d = check_base_reduce(state, ind, cfg)
    assert d.action == ActionType.SELL_REDUCE_BASE
    assert d.qty == 11   # 22 // 2


def test_base_not_double_reduced_when_ma20_and_drawdown_both_trigger():
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 900.0, 22),
        avg_entry_price=900.0,
        current_position_qty=22,
        highest_price_since_entry=1100.0,
    )
    # both rules fire — single reduce
    ind = make_indicators(adj_close=920.0, ma5=930, ma10=930, ma20=940, ma50=850)
    d = check_base_reduce(state, ind, cfg)
    assert d.qty == 11   # half once, not twice


def test_exit_all_when_close_below_ma50():
    cfg = base_config()
    state = make_state(
        state=BotState.REDUCE_MODE,
        base_lot=make_lot("base", 900.0, 22),
        addon_lots=[make_lot("add_1", 945.0, 8)],
        avg_entry_price=924.06,
        current_position_qty=30,
        highest_price_since_entry=1100.0,
    )
    ind = make_indicators(adj_close=850.0, ma5=900, ma10=900, ma20=900, ma50=900)
    d = check_base_reduce(state, ind, cfg)
    assert d.action == ActionType.EXIT_ALL
    assert d.qty == 30
    assert d.new_state == BotState.EXITED


def test_lifo_partial_reduce_exhausts_latest_lot_first():
    lots = [
        LotRecord(lot_id="a", entry_price=10, qty=5,
                  entry_date="2026-01-01"),
        LotRecord(lot_id="b", entry_price=20, qty=3,
                  entry_date="2026-01-02"),
        LotRecord(lot_id="c", entry_price=30, qty=4,
                  entry_date="2026-01-03"),
    ]
    updated, sold = lifo_reduce(lots, qty_to_sell=4)
    assert sold == 4
    # 'c' (latest, 4 qty) fully sold; 'a' and 'b' untouched
    assert len(updated) == 2
    assert updated[0].lot_id == "a"
    assert updated[1].lot_id == "b"
    assert updated[1].qty == 3


def test_lifo_reduce_splits_partially_exhausted_lot():
    lots = [
        LotRecord(lot_id="a", entry_price=10, qty=5,
                  entry_date="2026-01-01"),
        LotRecord(lot_id="b", entry_price=20, qty=3,
                  entry_date="2026-01-02"),
        LotRecord(lot_id="c", entry_price=30, qty=4,
                  entry_date="2026-01-03"),
    ]
    updated, sold = lifo_reduce(lots, qty_to_sell=6)
    assert sold == 6
    # c (4) consumed; b reduced by 2 → 1 remaining
    assert len(updated) == 2
    assert updated[1].lot_id == "b"
    assert updated[1].qty == 1


def test_avg_entry_recalculated_correctly_after_reduce():
    state = make_state(
        base_lot=make_lot("base", 900.0, 22),
        addon_lots=[
            make_lot("add_1", 945.0, 8),
            make_lot("add_2", 1000.0, 6),
        ],
    )
    recalc_avg_entry_price(state)
    # (22*900 + 8*945 + 6*1000) / 36 = (19800 + 7560 + 6000) / 36 = 33360 / 36 = 926.667
    assert abs(state.avg_entry_price - 926.6667) < 0.01
    assert state.current_position_qty == 36

    # Now drop the latest addon
    state.addon_lots = [make_lot("add_1", 945.0, 8)]
    recalc_avg_entry_price(state)
    # (22*900 + 8*945) / 30 = (19800 + 7560)/30 = 912.0
    assert abs(state.avg_entry_price - 912.0) < 0.01
    assert state.current_position_qty == 30


def test_reduce_mode_to_exited_when_base_lot_qty_reaches_zero():
    cfg = base_config()
    state = make_state(
        state=BotState.REDUCE_MODE,
        base_lot=make_lot("base", 900.0, 22),
        addon_lots=[],
        avg_entry_price=900.0,
        current_position_qty=22,
        highest_price_since_entry=1100.0,
    )
    # close < MA50 → Rule F exits all → state = EXITED
    ind = make_indicators(adj_close=800.0, ma5=900, ma10=900, ma20=900, ma50=900)
    d = check_base_reduce(state, ind, cfg)
    assert d.action == ActionType.EXIT_ALL
    assert d.new_state == BotState.EXITED
