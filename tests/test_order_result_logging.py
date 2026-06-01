from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from src.core.models import (
    ActionType,
    BotState,
    Decision,
    IndicatorSnapshot,
    OrderResult,
    OrderResultStatus,
    ThesisState,
)
from src.reporting.logger import log_cycle


def _indicators() -> IndicatorSnapshot:
    return IndicatorSnapshot(
        date=date(2026, 5, 31),
        adj_close=72329.0,
        open=72329.0,
        high=72329.0,
        low=72329.0,
        volume=0.0,
        prev_adj_close=72329.0,
        ma5=72329.0,
        ma10=72329.0,
        ma20=72329.0,
        ma50=72329.0,
        atr14=1000.0,
        avg_volume_20d=0.0,
        prior_highest_high_20d=72329.0,
        drawdown_from_20d_high=0.0,
        distance_from_ma10=0.0,
        distance_from_ma20=0.0,
        intraday_return=0.0,
        gap_up_pct=0.0,
        gap_down_pct=0.0,
        close=72329.0,
    )


def test_orders_csv_records_formatted_submitted_qty(tmp_path):
    state = ThesisState(symbol="BTC", state=BotState.STARTER_LONG)
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.0012443141755036015,
        reason="test",
        new_state=BotState.STARTER_LONG,
    )
    result = OrderResult(
        status=OrderResultStatus.FILLED,
        oid=1,
        action=ActionType.BUY_STARTER,
        side="buy",
        reduce_only=False,
        submitted_qty=0.00124,
        filled_qty=0.00124,
        remaining_qty=0.0,
        avg_fill_px=72325.0,
        limit_px=72401.0,
        raw_status_sanitized="filled",
        state_before=BotState.FLAT.value,
        intended_state_after=BotState.STARTER_LONG.value,
        applied_state_after=BotState.STARTER_LONG.value,
        created_at=datetime(2026, 5, 31, 10, 10, 21),
    )

    log_cycle(
        run_dir=str(tmp_path),
        state=state,
        decision=decision,
        fill=None,
        indicators=_indicators(),
        equity=1500.0,
        state_before=BotState.FLAT.value,
        state_after=BotState.STARTER_LONG.value,
        config_hash="abc123",
        order_result=result,
    )

    rows = pd.read_csv(tmp_path / "orders.csv")
    assert rows.iloc[-1]["qty"] == 0.00124
    assert rows.iloc[-1]["status"] == "filled"
