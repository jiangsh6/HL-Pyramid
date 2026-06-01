"""Section 22.8 — Stop tests."""
import math
from datetime import datetime, timezone

import pytest

from src.core.models import ActionType, BotState, PendingOrder
from src.core.decision_engine import run
from src.strategy.stops import (
    calc_initial_stop, check_intraday_drawdown, check_stop_triggers,
    hard_stop_price,
    update_trailing_stop,
)
from tests._helpers import base_config, make_indicators, make_lot, make_state


def test_initial_stop_uses_wider_of_pct_or_atr_stop():
    cfg = base_config()
    # Section 11 example: entry=900, stop_pct=12% → pct_stop=792
    # atr_stop = 900 - 2.5*50 = 775 → atr_stop wider (lower)
    s = calc_initial_stop(entry_price=900.0, atr14=50.0, config=cfg)
    assert math.isclose(s, 775.0)

    # Now reverse: small atr → pct stop is wider
    s2 = calc_initial_stop(entry_price=900.0, atr14=1.0, config=cfg)
    # atr_stop = 900 - 2.5 = 897.5; pct_stop = 792
    assert math.isclose(s2, 792.0)


def test_hard_stop_exits_all_and_sets_exited():
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 900.0, 30),
        avg_entry_price=900.0,
        current_position_qty=30,
        initial_stop_price=800.0,
    )
    ind = make_indicators(adj_close=790.0, ma5=850, ma10=850, ma20=850, ma50=850)
    d = run(state, ind, cfg)
    assert d.action == ActionType.SELL_STOP
    assert d.new_state == BotState.EXITED
    assert d.qty == 30


def test_hard_stop_uses_configurable_stop_loss_pct():
    cfg = base_config()
    cfg.risk["hard_stop"] = {"stop_loss_pct": 0.05}
    state = make_state(
        state=BotState.STARTER_LONG,
        base_lot=make_lot("base", 100.0, 1),
        avg_entry_price=100.0,
        current_position_qty=1,
    )

    assert hard_stop_price(state, cfg) == 95.0
    ind = make_indicators(adj_close=95.0)

    d = run(state, ind, cfg)

    assert d.action == ActionType.SELL_STOP
    assert d.qty == 1
    assert d.new_state == BotState.EXITED


@pytest.mark.parametrize("bot_state", [BotState.STARTER_LONG, BotState.PYRAMID_LONG, BotState.RUNNER_LONG])
def test_hard_stop_supported_for_long_states(bot_state):
    cfg = base_config()
    cfg.risk["hard_stop"] = {"stop_loss_pct": 0.05}
    state = make_state(
        state=bot_state,
        base_lot=make_lot("base", 100.0, 1),
        avg_entry_price=100.0,
        current_position_qty=1,
    )
    if bot_state == BotState.PYRAMID_LONG:
        state.addon_lots = [make_lot("add_1", 101.0, 1)]
        state.current_position_qty = 2
    ind = make_indicators(adj_close=94.0)

    d = run(state, ind, cfg)

    assert d.action == ActionType.SELL_STOP
    assert d.qty == state.current_position_qty


def test_hard_stop_not_triggered_above_configured_stop_price():
    cfg = base_config()
    cfg.risk["hard_stop"] = {"stop_loss_pct": 0.05}
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 1),
        avg_entry_price=100.0,
        current_position_qty=1,
    )
    ind = make_indicators(adj_close=95.01)

    d = run(state, ind, cfg)

    assert d.action != ActionType.SELL_STOP


def test_pending_reduce_only_exit_blocks_new_decisions():
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 1),
        avg_entry_price=100.0,
        current_position_qty=1,
        pending_order=PendingOrder(
            oid=1,
            action=ActionType.SELL_STOP.value,
            side="sell",
            reduce_only=True,
            qty=1,
            qty_submitted=1,
            qty_remaining=1,
            limit_px=95.0,
            status="submitted_unfilled",
            created_at=datetime.now(timezone.utc),
        ),
    )
    ind = make_indicators(adj_close=90.0)

    d = run(state, ind, cfg)

    assert d.action == ActionType.NO_ACTION
    assert d.reason == "pending_exit_resolution_required"
    assert "pending_exit_resolution_required" in d.blockers


def test_trailing_stop_updated_from_daily_high_before_trigger_check():
    cfg = base_config()
    # State holds an OLD trailing stop at 900; today's HIGH=1100 creates new stop.
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 800.0, 10),
        avg_entry_price=800.0,
        current_position_qty=10,
        highest_price_since_entry=1000.0,
        trailing_stop_price=900.0,
    )
    ind = make_indicators(
        adj_close=970.0, high=1100.0, ma5=950, ma10=940, ma20=920, ma50=900,
    )
    # Before run: trailing_stop=900. After Step 4: highest=1100; trailing_pct depends
    # on profit. profit = (970-800)/800 = 0.2125 → use tier with min_profit_pct=0.20
    # → trailing_pct=0.10 → candidate = 1100 * 0.90 = 990.  Trigger uses adj_close=970 ≤ 990.
    d = run(state, ind, cfg)
    assert state.highest_price_since_entry == 1100.0
    assert state.trailing_stop_price == 990.0
    assert d.action == ActionType.SELL_TRAILING_STOP


def test_trailing_stop_never_lowered():
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 800.0, 10),
        avg_entry_price=800.0,
        current_position_qty=10,
        highest_price_since_entry=1200.0,
        trailing_stop_price=1100.0,   # existing high stop
    )
    # New bar with lower high → candidate stop lower → should NOT lower
    ind = make_indicators(adj_close=1080.0, high=1100.0, ma5=1050)
    update_trailing_stop(state, ind, cfg)
    # candidate = max(1200, 1100) * (1 - trailing) = 1200*0.88 = 1056; stays at 1100
    assert state.trailing_stop_price == 1100.0


def test_trailing_stop_trigger_uses_adj_close_not_intraday():
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 800.0, 10),
        avg_entry_price=800.0,
        current_position_qty=10,
        initial_stop_price=500.0,
        trailing_stop_price=900.0,
        highest_price_since_entry=950.0,
    )
    # adj_close=950 > trailing_stop_price even though low=850 dipped under
    ind = make_indicators(adj_close=950.0, low=850.0, high=950.0, ma5=900)
    trig = check_stop_triggers(state, ind, cfg)
    assert trig is None


def test_trailing_stop_correct_when_bar_makes_new_high_then_closes_below():
    """Step 4 must update highest_price_since_entry from bar HIGH BEFORE checking trigger."""
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 800.0, 10),
        avg_entry_price=800.0,
        current_position_qty=10,
        highest_price_since_entry=1000.0,
        trailing_stop_price=940.0,
    )
    # Today: new high 1100, then close drops to 935.
    ind = make_indicators(adj_close=935.0, high=1100.0, ma5=950)
    d = run(state, ind, cfg)
    # candidate_stop = 1100 * (1 - 0.12 default) = 968; raised to 968.
    # Close 935 ≤ 968 → trailing stop fires
    assert state.trailing_stop_price >= 968.0 - 1e-6
    assert d.action == ActionType.SELL_TRAILING_STOP


def test_max_thesis_loss_exits_and_halts_bot():
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 900.0, 10),
        avg_entry_price=900.0,
        current_position_qty=10,
        thesis_pnl=0.0,   # stale value must be replaced by mark-to-market loss
    )
    ind = make_indicators(adj_close=200.0, ma5=250, ma10=250, ma20=250, ma50=250)
    d = run(state, ind, cfg)
    assert d.action == ActionType.EXIT_ALL
    assert d.new_state == BotState.HALTED
    assert state.halted is True
    assert state.halt_reason == "max_thesis_loss"


def test_max_daily_loss_blocks_orders_only():
    cfg = base_config()
    # FLAT state, no position. Hit daily loss threshold via daily_pnl.
    state = make_state(realized_pnl=10_000.0, daily_pnl=-3_500.0)   # > 3% of 100_000
    ind = make_indicators(adj_close=100.0, ma5=99, ma10=98, ma20=95, ma50=90,
                          volume=2_000_000, avg_volume_20d=1_500_000)
    d = run(state, ind, cfg)
    assert d.action == ActionType.NO_ACTION
    assert "daily_loss_limit" in d.blockers
    assert state.halted is False   # non-terminal


def test_intraday_drawdown_halts_bot():
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 1000.0, 100),
        avg_entry_price=1000.0,
        current_position_qty=100,
    )
    # max_intraday_drawdown_pct_of_equity = 0.04 → $4000 budget on 100k.
    # 100 qty * (current - 1000) ≤ -4000 → current ≤ 960
    assert check_intraday_drawdown(state, current_price=950.0, config=cfg) is True
    assert check_intraday_drawdown(state, current_price=970.0, config=cfg) is False
