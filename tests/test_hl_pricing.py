from __future__ import annotations

import pytest

from src.hl.precision import validate_hl_order_wire
from src.hl.pricing import compute_oracle_safe_entry_price, compute_oracle_safe_exit_price


def test_sell_exit_price_within_max_oracle_deviation():
    result = compute_oracle_safe_exit_price(
        "sell",
        mark_price=71738.0,
        oracle_price=71738.0,
        sz_decimals=5,
        max_oracle_deviation_bps=5,
        aggressiveness_bps=2,
    )

    lower_bound = 71738.0 * (1 - 5 / 10_000)
    assert result.formatted_exit_px >= lower_bound
    assert result.formatted_exit_px <= 71738.0


def test_sell_exit_price_formats_for_btc_sz_decimals():
    result = compute_oracle_safe_exit_price(
        "sell",
        mark_price=71738.0,
        oracle_price=71738.0,
        sz_decimals=5,
        max_oracle_deviation_bps=5,
        aggressiveness_bps=2,
    )

    assert result.formatted_exit_px == pytest.approx(71724.0)
    validate_hl_order_wire(
        __import__("decimal").Decimal(str(result.formatted_exit_px)),
        __import__("decimal").Decimal("0.00125"),
        5,
        is_perp=True,
    )


def test_previous_bad_price_would_exceed_tight_deviation_and_is_capped():
    result = compute_oracle_safe_exit_price(
        "sell",
        mark_price=71738.0,
        oracle_price=71738.0,
        sz_decimals=5,
        max_oracle_deviation_bps=5,
        aggressiveness_bps=10,
    )

    lower_bound = 71738.0 * (1 - 5 / 10_000)
    assert result.raw_exit_px == pytest.approx(71666.262)
    assert result.formatted_exit_px >= lower_bound
    assert result.formatted_exit_px == pytest.approx(71703.0)


def test_buy_exit_price_within_max_oracle_deviation_for_future_short_support():
    result = compute_oracle_safe_exit_price(
        "buy",
        mark_price=100.0,
        oracle_price=100.0,
        sz_decimals=5,
        max_oracle_deviation_bps=5,
        aggressiveness_bps=20,
    )

    assert result.formatted_exit_px <= 100.0 * (1 + 5 / 10_000)


def test_aggressive_buy_starter_price_is_above_mark():
    result = compute_oracle_safe_entry_price(
        "buy",
        mark_price=72000.0,
        oracle_price=72000.0,
        sz_decimals=5,
        max_oracle_deviation_bps=20,
        aggressiveness_bps=5,
    )

    assert result.formatted_exit_px >= 72000.0


def test_aggressive_buy_starter_price_stays_within_max_oracle_deviation():
    result = compute_oracle_safe_entry_price(
        "buy",
        mark_price=72000.0,
        oracle_price=72000.0,
        sz_decimals=5,
        max_oracle_deviation_bps=20,
        aggressiveness_bps=5,
    )

    assert result.formatted_exit_px <= 72000.0 * (1 + 20 / 10_000)


def test_aggressive_buy_starter_price_formats_for_btc():
    result = compute_oracle_safe_entry_price(
        "buy",
        mark_price=72000.0,
        oracle_price=72000.0,
        sz_decimals=5,
        max_oracle_deviation_bps=20,
        aggressiveness_bps=5,
    )

    validate_hl_order_wire(
        __import__("decimal").Decimal(str(result.formatted_exit_px)),
        __import__("decimal").Decimal("0.005"),
        5,
        is_perp=True,
    )
