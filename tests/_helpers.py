"""Shared test fixtures and factories for Phase 2 tests."""
from __future__ import annotations

from datetime import date
from typing import Any

from src.core.config_loader import load_config
from src.core.models import (
    BotConfig, BotState, IndicatorSnapshot, LotRecord, ThesisState,
)


CONFIG_PATH = "config/mu_long_thesis.yaml"


def base_config() -> BotConfig:
    return load_config(CONFIG_PATH)


def make_indicators(**kwargs: Any) -> IndicatorSnapshot:
    """Build an IndicatorSnapshot with neutral defaults; override via kwargs."""
    defaults = dict(
        date=date(2026, 5, 27),
        adj_close=100.0,
        open=99.0,
        high=101.0,
        low=98.0,
        volume=2_000_000.0,
        prev_adj_close=99.5,
        ma5=99.0,
        ma10=98.0,
        ma20=95.0,
        ma50=90.0,
        atr14=2.0,
        avg_volume_20d=1_500_000.0,
        prior_highest_high_20d=99.0,
        drawdown_from_20d_high=0.0,
        distance_from_ma10=0.02,
        distance_from_ma20=0.05,
        intraday_return=0.01,
        gap_up_pct=0.0,
        gap_down_pct=0.0,
    )
    defaults.update(kwargs)
    return IndicatorSnapshot(**defaults)


def make_state(state: BotState = BotState.FLAT, **kwargs: Any) -> ThesisState:
    defaults: dict[str, Any] = dict(symbol="MU", state=state)
    defaults.update(kwargs)
    return ThesisState(**defaults)


def make_lot(
    lot_id: str,
    entry_price: float,
    qty: int,
    entry_date_str: str = "2026-05-20",
) -> LotRecord:
    return LotRecord(
        lot_id=lot_id,
        entry_price=entry_price,
        qty=qty,
        entry_date=date.fromisoformat(entry_date_str),
    )
