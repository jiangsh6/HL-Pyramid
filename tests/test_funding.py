from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from src.backtest.backtester import BacktestRecord, bar_interval_to_hours
from src.backtest.metrics import compute_metrics
from src.core.models import ActionType, BotState
from src.hl.funding import (
    apply_funding_to_state,
    calc_funding_payment,
    get_funding_history,
    get_predicted_funding,
)
from src.reporting.daily_summary import format_summary
from src.risk.validators import WARN, validate_data
from tests._helpers import base_config, make_indicators, make_lot, make_state


def test_get_funding_history_parses_response():
    client = MagicMock()
    client.post_info.return_value = [
        {
            "coin": "BTC",
            "time": 1_700_000_000_000,
            "fundingRate": "0.0001",
            "positionUsd": "50000",
        }
    ]

    payments = get_funding_history("BTC", 1, 2, client)

    assert len(payments) == 1
    payment = payments[0]
    assert payment.coin == "BTC"
    assert payment.timestamp == datetime.fromtimestamp(
        1_700_000_000_000 / 1000, tz=timezone.utc
    )
    assert payment.funding_rate == 0.0001
    assert payment.position_usd == 50000.0
    assert payment.payment_usd == -5.0
    client.post_info.assert_called_once_with({
        "type": "fundingHistory",
        "coin": "BTC",
        "startTime": 1,
        "endTime": 2,
    })


def test_get_funding_history_returns_empty_on_no_data():
    client = MagicMock()
    client.post_info.return_value = []
    assert get_funding_history("BTC", 1, 2, client) == []

    client.post_info.side_effect = RuntimeError("api down")
    assert get_funding_history("BTC", 1, 2, client) == []


def test_get_predicted_funding_returns_rate():
    client = MagicMock()
    client.post_info.return_value = [
        {"universe": [{"name": "ETH"}, {"name": "BTC"}]},
        [{"funding": "0.0002"}, {"funding": "0.0001"}],
    ]
    assert get_predicted_funding("BTC", client) == 0.0001


def test_get_predicted_funding_returns_none_on_error():
    client = MagicMock()
    client.post_info.side_effect = RuntimeError("api down")
    assert get_predicted_funding("BTC", client) is None


def test_calc_funding_payment_long_pays_positive_rate():
    payment = calc_funding_payment(0.0001, position_contracts=2.0, mark_price=50000.0)
    assert payment == -10.0


def test_calc_funding_payment_long_receives_negative_rate():
    payment = calc_funding_payment(-0.0001, position_contracts=2.0, mark_price=50000.0)
    assert payment == 10.0


def test_calc_funding_payment_zero_when_no_position():
    assert calc_funding_payment(0.0001, position_contracts=0.0, mark_price=50000.0) == 0.0


def test_calc_funding_payment_scales_with_hours():
    one_hour = calc_funding_payment(0.0001, 1.0, 50000.0, hours=1.0)
    four_hours = calc_funding_payment(0.0001, 1.0, 50000.0, hours=4.0)
    assert four_hours == 4 * one_hour


def test_apply_funding_to_state_updates_three_fields():
    state = make_state(
        cumulative_funding_pnl=-1.0,
        thesis_pnl=100.0,
        unrealized_pnl=50.0,
    )
    updated = apply_funding_to_state(state, -12.34)
    assert updated.cumulative_funding_pnl == -13.34
    assert updated.thesis_pnl == 87.66
    assert updated.unrealized_pnl == 37.66


def test_apply_funding_to_state_is_pure():
    state = make_state(
        cumulative_funding_pnl=0.0,
        thesis_pnl=100.0,
        unrealized_pnl=50.0,
    )
    before = state.model_copy(deep=True)
    updated = apply_funding_to_state(state, -10.0)
    assert state == before
    assert updated is not state
    assert updated.thesis_pnl == 90.0


def test_funding_pnl_in_backtest_metrics():
    cfg = base_config()
    records = [
        BacktestRecord(
            bar_date=date(2026, 1, 1),
            adj_close=100.0,
            open_price=100.0,
            shares=1,
            avg_entry_price=100.0,
            fill_price=None,
            fill_shares=0,
            action=ActionType.NO_ACTION,
            state_name="BASE_LONG",
            in_event_window=False,
            exposure_pct=0.01,
            trailing_stop_price=None,
            gap_loss=False,
            funding_payment=-1.25,
        ),
        BacktestRecord(
            bar_date=date(2026, 1, 2),
            adj_close=100.0,
            open_price=100.0,
            shares=1,
            avg_entry_price=100.0,
            fill_price=None,
            fill_shares=0,
            action=ActionType.NO_ACTION,
            state_name="BASE_LONG",
            in_event_window=False,
            exposure_pct=0.01,
            trailing_stop_price=None,
            gap_loss=False,
            funding_payment=0.25,
        ),
    ]
    metrics = compute_metrics(records, cfg)
    assert metrics["funding_pnl"] == -1.0


def test_high_funding_rate_warn_in_validators():
    cfg = base_config()
    indicators = make_indicators()
    state = make_state(last_funding_rate=0.0006)
    issues = validate_data(indicators, state, cfg)
    assert any(
        issue.severity == WARN
        and "High funding rate: 0.0600%/hr. Holding cost is elevated." in issue.reason
        for issue in issues
    )


def test_bar_interval_to_hours_all_intervals():
    assert bar_interval_to_hours("1m") == 1 / 60
    assert bar_interval_to_hours("5m") == 5 / 60
    assert bar_interval_to_hours("15m") == 0.25
    assert bar_interval_to_hours("1h") == 1.0
    assert bar_interval_to_hours("4h") == 4.0
    assert bar_interval_to_hours("1d") == 24.0


def test_funding_shown_in_daily_summary_when_nonzero():
    cfg = base_config()
    indicators = make_indicators()
    state = make_state(
        state=BotState.BASE_LONG,
        base_lot=make_lot("base", 100.0, 1),
        current_position_shares=1,
        avg_entry_price=100.0,
        cumulative_funding_pnl=-12.34,
        last_funding_rate=0.0001,
    )
    summary = format_summary(state, indicators, cfg)
    assert "--- Funding ---" in summary
    assert "Cumulative Funding PnL:  $-12.34" in summary
    assert "Last Funding Rate:        0.0100%/hr" in summary


def test_funding_hidden_in_daily_summary_when_zero():
    cfg = base_config()
    indicators = make_indicators()
    state = make_state()
    summary = format_summary(state, indicators, cfg)
    assert "--- Funding ---" not in summary
