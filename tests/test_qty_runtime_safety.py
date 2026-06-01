from __future__ import annotations

import math
from datetime import date
from pathlib import Path

from src.core.decision_engine import run as engine_run
from src.core.models import EPSILON, ActionType, BotState, Decision
from src.execution.paper_broker import execute
from src.reporting.state_writer import apply_fill, load_state_or_halt, read_state
from src.risk.risk_manager import check_order_allowed
from src.strategy.event_risk import check_event_derisking
from src.strategy.reduce import check_addon_reduce, lifo_reduce
from src.strategy.sizing import calc_entry_size
from src.strategy.take_profit import check_layered_tp, check_target_tp
from tests._helpers import base_config, make_indicators, make_lot, make_state


def _fractional_long():
    return make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 70_000.0, 0.001),
        current_position_qty=0.001,
        avg_entry_price=70_000.0,
        initial_stop_price=68_000.0,
        trailing_stop_price=69_000.0,
    )


def test_fractional_position_active_without_share_field():
    state = _fractional_long()
    assert state.current_position_qty > EPSILON
    assert not hasattr(state, "current_position_shares")


def test_hard_stop_exits_fractional_qty():
    decision = engine_run(_fractional_long(), make_indicators(adj_close=67_900.0), base_config())
    assert decision.action == ActionType.SELL_STOP
    assert math.isclose(decision.qty, 0.001)


def test_trailing_stop_exits_fractional_qty():
    state = _fractional_long()
    state.initial_stop_price = 60_000.0
    decision = engine_run(state, make_indicators(adj_close=68_900.0, high=70_000.0), base_config())
    assert decision.action == ActionType.SELL_TRAILING_STOP
    assert math.isclose(decision.qty, 0.001)


def test_reduce_latest_addon_uses_fractional_qty():
    cfg = base_config()
    state = make_state(
        state=BotState.PYRAMID_LONG,
        base_lot=make_lot("base", 70_000.0, 0.001),
        addon_lots=[make_lot("add_1", 71_000.0, 0.0005)],
        current_position_qty=0.0015,
        avg_entry_price=70_333.333333,
        highest_price_since_entry=72_000.0,
    )
    decision = check_addon_reduce(state, make_indicators(adj_close=69_000.0, ma5=70_000.0), cfg)
    assert decision is not None
    assert math.isclose(decision.qty, 0.0005)
    updated, sold = lifo_reduce(state.addon_lots, decision.qty, sz_decimals=6)
    assert math.isclose(sold, 0.0005)
    assert updated == []
    assert math.isclose(state.base_lot.qty, 0.001)


def test_reduce_all_addons_keeps_base_qty():
    lots = [make_lot("add_1", 71_000.0, 0.0005), make_lot("add_2", 72_000.0, 0.0004)]
    updated, sold = lifo_reduce(lots, 0.0009, sz_decimals=6)
    assert math.isclose(sold, 0.0009)
    assert updated == []


def test_take_profit_uses_fractional_qty_and_runner_qty():
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 70_000.0, 1.0),
        current_position_qty=1.0,
        original_base_qty=1.0,
        avg_entry_price=70_000.0,
    )
    layered = check_layered_tp(state, make_indicators(adj_close=91_000.0), cfg)
    assert layered is not None
    assert layered.qty > 0
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 70_000.0, 1.0),
        current_position_qty=1.0,
        original_base_qty=1.0,
        avg_entry_price=70_000.0,
    )
    target = check_target_tp(state, make_indicators(adj_close=2_000.0), cfg)
    assert target is not None
    assert state.runner_target_qty is not None
    assert not hasattr(state, "runner_target_shares")


def test_event_risk_derisks_to_fractional_target_qty():
    cfg = base_config()
    cfg.event_risk["enabled"] = True
    cfg.event_risk["use_calendar_days"] = True
    cfg.thesis["event_date"] = "2026-06-05"
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 70_000.0, 1.0),
        current_position_qty=1.0,
        avg_entry_price=70_000.0,
    )
    decision = check_event_derisking(state, make_indicators(date=date(2026, 6, 3), adj_close=70_000.0), cfg)
    assert decision is not None
    assert decision.qty > 0
    assert decision.qty < 1.0


def test_exposure_cap_uses_qty_notional():
    cfg = base_config()
    state = make_state(current_position_qty=0.001)
    decision = Decision(action=ActionType.BUY_ADDON, qty=0.0, reason="noop")
    indicators = make_indicators(adj_close=70_000.0)
    allowed, reason = check_order_allowed(state, decision, cfg, indicators)
    assert allowed is True
    assert reason == "ok"
    assert math.isclose(state.current_position_qty * indicators.adj_close, 70.0)


def test_sizing_does_not_round_fractional_btc_qty_to_zero_or_one():
    cfg = base_config()
    qty, blocker = calc_entry_size(
        make_state(),
        cfg,
        entry_price=70_000.0,
        atr14=100.0,
        intended_exposure_pct=100.0 / 75_000.0,
        sz_decimals=8,
    )
    assert blocker is None
    assert math.isclose(qty, 100.0 / 70_000.0, rel_tol=1e-6)
    assert 0.0 < qty < 1.0


def test_sell_fill_rejects_oversell_beyond_tiny_tolerance():
    state = _fractional_long()
    fill = execute(Decision(action=ActionType.EXIT_ALL, qty=0.001, reason="test"), 71_000.0, state, base_config())
    apply_fill(state, fill)
    assert state.current_position_qty == 0.0


def test_runtime_files_do_not_use_forbidden_share_identifiers():
    forbidden = (
        "current_position_shares",
        "base_position_shares",
        "original_base_shares",
        "lot.shares",
        "shares_to_sell",
        "shares_to_buy",
    )
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for folder in ("src", "scripts"):
        for path in (root / folder).rglob("*.py"):
            text = path.read_text()
            for token in forbidden:
                if token in text:
                    offenders.append(f"{path.relative_to(root)}:{token}")
    assert offenders == []


def test_legacy_share_state_loads_as_halted(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"symbol":"BTC","current_position_shares":1,'
        '"base_lot":{"lot_id":"base","entry_price":70000,"shares":1,"entry_date":"2026-05-30"}}'
    )
    state = read_state(str(state_path))
    assert state.state == BotState.HALTED
    assert state.halted is True
    assert state.halt_reason == "legacy_share_state_not_supported"


def test_corrupted_state_load_writes_halted_state(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{not-json")
    state, terminal = load_state_or_halt(str(state_path), "BTC")
    assert terminal is True
    assert state.state == BotState.HALTED
    assert state.halt_reason == "state_load_or_validation_error"
    persisted = read_state(str(state_path))
    assert persisted.state == BotState.HALTED
    assert persisted.halt_reason == "state_load_or_validation_error"


def test_schema_invalid_state_load_writes_halted_state(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"symbol":"BTC","current_position_qty":-1}')
    state, terminal = load_state_or_halt(str(state_path), "BTC")
    assert terminal is True
    assert state.state == BotState.HALTED
    assert state.halt_reason == "state_load_or_validation_error"
