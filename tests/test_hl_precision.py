from __future__ import annotations

from decimal import Decimal

import pytest

from src.hl.precision import (
    validate_hl_order_wire,
    format_hl_price,
    format_hl_qty,
)


def test_btc_perp_price_formats_without_float_to_wire_rounding():
    px = format_hl_price(72440.36799999999, 5, is_perp=True, side="buy")
    assert px == Decimal("72440")
    validate_hl_order_wire(px, Decimal("0.00124"), 5, is_perp=True)


def test_btc_perp_qty_rounds_down_to_sz_decimals():
    qty = format_hl_qty(0.0012436435993809418, 5, rounding="down")
    assert qty == Decimal("0.00124")
    validate_hl_order_wire(Decimal("72440"), qty, 5, is_perp=True)


def test_high_price_integer_is_allowed():
    px = format_hl_price(72440.36799999999, 5, is_perp=True, side="sell")
    assert px == Decimal("72441")
    validate_hl_order_wire(px, Decimal("0.00124"), 5, is_perp=True)


def test_eth_like_perp_uses_sig_figs_rule():
    px = format_hl_price(3567.8912, 4, is_perp=True, side="buy")
    # 5 sig figs with perp decimal cap 2 => 3567.8
    assert px == Decimal("3567.8")
    validate_hl_order_wire(px, Decimal("0.1234"), 4, is_perp=True)


def test_buy_vs_sell_rounding_policy_is_conservative():
    buy_px = format_hl_price(72440.36799999999, 5, is_perp=True, side="buy")
    sell_px = format_hl_price(72440.36799999999, 5, is_perp=True, side="sell")
    assert buy_px <= Decimal("72440.36799999999")
    assert sell_px >= Decimal("72440.36799999999")


def test_qty_below_min_after_rounding_is_detectable():
    qty = format_hl_qty(0.0009999, 5, rounding="down")
    assert qty == Decimal("0.00099")
    qty2 = format_hl_qty(0.0000099, 5, rounding="down")
    assert qty2 == Decimal("0")
