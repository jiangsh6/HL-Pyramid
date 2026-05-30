"""
HL Phase 5 — Event risk calendar-days mode tests.

All 4 named tests:
  test_calc_trading_days_uses_busday_count_by_default
  test_calc_calendar_days_counts_all_days
  test_calendar_days_includes_weekends
  test_update_event_risk_uses_config_flag
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from src.strategy.event_risk import calc_trading_days_to_event, update_event_risk_mode
from tests._helpers import make_indicators, make_state


# ── Named tests ───────────────────────────────────────────────────────────────

def test_calc_trading_days_uses_busday_count_by_default():
    """Without use_calendar_days, a Mon→Mon span skips weekends."""
    # 2026-06-01 (Mon) to 2026-06-08 (Mon) = 5 business days
    today      = date(2026, 6, 1)
    event_date = date(2026, 6, 8)
    result = calc_trading_days_to_event(today, event_date)
    assert result == 5


def test_calc_calendar_days_counts_all_days():
    """use_calendar_days=True returns the raw calendar difference."""
    today      = date(2026, 6, 1)
    event_date = date(2026, 6, 8)
    result = calc_trading_days_to_event(today, event_date, use_calendar_days=True)
    assert result == 7


def test_calendar_days_includes_weekends():
    """A Fri→Mon span: business days = 1, calendar days = 3."""
    today      = date(2026, 6, 5)   # Friday
    event_date = date(2026, 6, 8)   # Monday
    assert calc_trading_days_to_event(today, event_date) == 1
    assert calc_trading_days_to_event(today, event_date, use_calendar_days=True) == 3


def test_update_event_risk_uses_config_flag():
    """
    When use_calendar_days=True in the config, update_event_risk_mode uses
    calendar days for the threshold comparison.

    We pick a date that is 3 calendar days before the event but 0 business days
    (weekend): with use_calendar_days=True it should be within the 10-day
    threshold and trigger EVENT_RISK_MODE; with the default business-day mode
    it would be 0 days and also trigger (both within threshold).

    The key assertion is that the function reads the flag from config without
    raising and that days_to_event is the calendar count (3) not business (1).
    """
    from src.core.config_loader import load_config
    from src.core.models import BotState

    config = load_config("config/btc_long_thesis.yaml")
    # Enable event_risk with use_calendar_days
    config.event_risk["enabled"] = True
    config.event_risk["use_calendar_days"] = True
    config.thesis["event_date"] = "2026-06-08"  # Monday

    state      = make_state(BotState.BASE_LONG, symbol="BTC")
    indicators = make_indicators(date=date(2026, 6, 5))  # Friday — 3 calendar days away

    update_event_risk_mode(state, indicators, config)

    # Calendar days: (2026-06-08) - (2026-06-05) = 3 days
    assert state.days_to_event == 3


# ── Extra coverage ────────────────────────────────────────────────────────────

def test_calc_calendar_days_negative_when_past():
    """Calendar days are negative after the event date."""
    today      = date(2026, 6, 10)
    event_date = date(2026, 6, 8)
    result = calc_trading_days_to_event(today, event_date, use_calendar_days=True)
    assert result == -2
