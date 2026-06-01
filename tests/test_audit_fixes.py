from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.decision_engine import run as engine_run
from src.core.models import ActionType, BotState, Decision, Fill, LotRecord, ThesisState
from src.execution.hl_broker import execute_hl
from src.hl.client import HyperliquidClient
from src.hl.funding import get_funding_history
from src.hl.order_placer import HLOrderRequest, HLOrderResponse, place_order
from src.reporting.state_writer import mark_to_market_state, reset_state_to_flat
from src.risk.validators import HALT, WARN, validate_data
from tests._helpers import base_config, make_indicators, make_lot, make_state


FAKE_KEY = "0x" + "a" * 64


def _fake_post_response():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"status": "ok", "response": {"data": {"statuses": []}}}
    return response


def _assert_place_order_is_mainnet(network: str, expected: bool) -> None:
    client = HyperliquidClient(network=network)
    client.post_info = MagicMock(return_value={"universe": [{"name": "BTC"}]})  # type: ignore[method-assign]
    request = HLOrderRequest(
        coin="BTC",
        is_buy=True,
        sz=0.1,
        limit_px=50000.0,
        order_type={"limit": {"tif": "Gtc"}},
    )

    with patch("src.hl.auth.build_signer", return_value=object()), \
         patch("hyperliquid.utils.signing.order_request_to_order_wire", return_value={"a": 0}), \
         patch("hyperliquid.utils.signing.order_wires_to_order_action", return_value={"type": "order"}), \
         patch("hyperliquid.utils.signing.get_timestamp_ms", return_value=123), \
         patch("hyperliquid.utils.signing.sign_l1_action", return_value={"r": "0x1", "s": "0x2", "v": 27}) as sign, \
         patch("src.hl.order_placer._requests.post", return_value=_fake_post_response()):
        response = place_order(request, "0x1234", FAKE_KEY, client)

    assert response.status == "ok"
    assert sign.call_args.args[5] is expected


def test_is_mainnet_false_on_testnet_orders():
    _assert_place_order_is_mainnet("testnet", False)


def test_is_mainnet_true_on_mainnet_orders():
    _assert_place_order_is_mainnet("mainnet", True)


def test_daily_pnl_resets_on_new_day():
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 1),
        current_position_qty=1.0,
        avg_entry_price=100.0,
        initial_stop_price=1.0,
        trailing_stop_price=1.0,
        realized_pnl=5000.0,
        daily_pnl=-4000.0,
        last_updated=datetime(2026, 5, 26, 16, 0),
    )
    indicators = make_indicators(date=date(2026, 5, 27), adj_close=100.0)

    decision = engine_run(state, indicators, cfg)

    assert state.daily_pnl == 0.0
    assert "daily_loss_limit" not in decision.blockers

    state.daily_pnl = -4000.0
    state.last_updated = datetime(2026, 5, 27, 10, 0)
    decision = engine_run(state, indicators, cfg)
    assert "daily_loss_limit" in decision.blockers


def test_daily_pnl_independent_of_cumulative_pnl():
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 1),
        current_position_qty=1.0,
        avg_entry_price=100.0,
        initial_stop_price=1.0,
        trailing_stop_price=1.0,
        realized_pnl=10000.0,
        daily_pnl=-4000.0,
        last_updated=datetime(2026, 5, 27, 10, 0),
    )
    decision = engine_run(state, make_indicators(date=date(2026, 5, 27)), cfg)
    assert "daily_loss_limit" in decision.blockers


def test_reduce_mode_transitions_to_base_long_after_addon_removal():
    cfg = base_config()
    state = make_state(
        state=BotState.REDUCE_MODE,
        base_lot=make_lot("base", 100.0, 10),
        addon_lots=[],
        current_position_qty=10.0,
        avg_entry_price=100.0,
        highest_price_since_entry=100.0,
        initial_stop_price=1.0,
        trailing_stop_price=1.0,
    )
    decision = engine_run(state, make_indicators(adj_close=100.0), cfg)
    assert decision.action == ActionType.NO_ACTION
    assert state.state == BotState.BASE_LONG


def test_reset_state_clears_all_hl_fields():
    state = make_state(
        state=BotState.HALTED,
        cumulative_funding_pnl=-10.0,
        liquidation_price=99.0,
        margin_used_usd=100.0,
        leverage_used=3.0,
        last_funding_rate=0.001,
        next_funding_timestamp=datetime(2026, 5, 27, 12, 0),
        runner_target_qty=1,
        original_base_qty=2.0,
        sz_decimals=3,
        daily_pnl=-100.0,
    )
    reset_state_to_flat(state)
    assert state.cumulative_funding_pnl == 0.0
    assert state.liquidation_price is None
    assert state.margin_used_usd == 0.0
    assert state.leverage_used == 0.0
    assert state.last_funding_rate == 0.0
    assert state.next_funding_timestamp is None
    assert state.runner_target_qty is None
    assert state.original_base_qty == 0.0
    assert state.sz_decimals == 0
    assert state.daily_pnl == 0.0


def test_decision_cycle_marks_to_market_before_max_thesis_loss():
    cfg = base_config()
    cfg.risk["max_loss"]["max_thesis_loss_pct_of_equity"] = 0.00005
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 1.0),
        current_position_qty=1.0,
        original_base_qty=1.0,
        avg_entry_price=100.0,
        initial_stop_price=1.0,
        trailing_stop_price=1.0,
        realized_pnl=0.0,
        thesis_pnl=0.0,
    )
    decision = engine_run(state, make_indicators(adj_close=90.0, high=90.0), cfg)
    assert state.unrealized_pnl == -10.0
    assert state.thesis_pnl == -10.0
    assert decision.action == ActionType.EXIT_ALL
    assert decision.reason == "max_thesis_loss"


def test_mark_to_market_includes_realized_unrealized_and_funding():
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 1.0),
        current_position_qty=1.0,
        avg_entry_price=100.0,
        realized_pnl=-5.0,
        cumulative_funding_pnl=-1.0,
        thesis_pnl=0.0,
    )
    mark_to_market_state(state, 90.0)
    assert state.unrealized_pnl == -10.0
    assert state.thesis_pnl == -16.0


def test_consistency_check_uses_contracts_not_share_fields():
    cfg = base_config()
    lot = LotRecord(
        lot_id="base",
        entry_price=50000.0,
        qty=0.001,
        entry_date=date(2026, 5, 27),
    )
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=lot,
        current_position_qty=0.001,
        avg_entry_price=50000.0,
    )
    decision = engine_run(state, make_indicators(), cfg)
    assert decision.action != ActionType.HALT
    assert not state.halted


def test_consistency_check_halts_on_contracts_mismatch():
    cfg = base_config()
    lot = LotRecord(
        lot_id="base",
        entry_price=50000.0,
        qty=0.001,
        entry_date=date(2026, 5, 27),
    )
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=lot,
        current_position_qty=0.002,
        avg_entry_price=50000.0,
    )
    decision = engine_run(state, make_indicators(), cfg)
    assert decision.action == ActionType.HALT
    assert state.halted
    assert state.halt_reason == "state_consistency_error"


def test_liquidation_above_stop_is_halt_not_warn():
    cfg = base_config()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 1),
        current_position_qty=1,
        initial_stop_price=90.0,
        liquidation_price=95.0,
    )
    issues = validate_data(make_indicators(), state, cfg)
    assert any(
        issue.severity == HALT and "liquidation_price_above_stop" in issue.reason
        for issue in issues
    )
    assert not any(
        issue.severity == WARN and "liquidation_price_above_stop" in issue.reason
        for issue in issues
    )


def test_hl_broker_realized_pnl_nonzero_on_sell():
    cfg = base_config()
    cfg.hl = {"coin": "BTC"}
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=LotRecord(
            lot_id="base",
            entry_price=100.0,
            qty=0.5,
            entry_date=date(2026, 5, 27),
        ),
        current_position_qty=0.5,
        avg_entry_price=100.0,
    )
    decision = Decision(action=ActionType.EXIT_ALL, qty=0.5, reason="test")
    client = HyperliquidClient(network="testnet")

    with patch("src.execution.hl_broker.place_order", return_value=HLOrderResponse(
        status="ok",
        filled_sz=0.5,
        avg_fill_px=120.0,
    )):
        fill = execute_hl(decision, state, cfg, client, "0x1234", FAKE_KEY, mark_price=120.0)

    assert fill.realized_pnl == 10.0


def test_funding_history_field_name_fallback():
    client = MagicMock()
    client.post_info.return_value = [
        {
            "coin": "BTC",
            "time": 1_700_000_000_000,
            "funding": "0.0001",
            "positionUsd": "50000",
        }
    ]
    payments = get_funding_history("BTC", 1, 2, client)
    assert payments[0].funding_rate == 0.0001
    assert payments[0].payment_usd == -5.0


def test_requirements_txt_exists_and_lists_all_11_dependencies():
    reqs = Path("requirements.txt").read_text()
    for dep in [
        "pydantic>=2.0",
        "yfinance>=0.2.40",
        "pandas>=2.0",
        "numpy>=1.26",
        "pytest>=7.0",
        "pyyaml>=6.0",
        "requests>=2.28",
        "websocket-client>=1.6",
        "eth-account>=0.10",
        "hyperliquid-python-sdk>=0.5",
        "matplotlib>=3.8",
    ]:
        assert dep in reqs
