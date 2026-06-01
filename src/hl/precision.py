from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
from typing import Literal

from src.core.models import EPSILON

MAX_DECIMALS_PERP = 6
MAX_DECIMALS_SPOT = 8

PriceSide = Literal["buy", "sell"]
QtyRounding = Literal["down", "up"]


def _to_decimal(value: float | str | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _rounding_for_price(side: PriceSide | None) -> str:
    if side == "sell":
        return ROUND_UP
    return ROUND_DOWN


def _quantum_for_sig_figs(px: Decimal, sig_figs: int = 5) -> Decimal:
    if px == 0:
        return Decimal("1")
    exponent = px.adjusted() - (sig_figs - 1)
    return Decimal(f"1e{exponent}")


def _quantum_for_decimals(max_decimals: int) -> Decimal:
    if max_decimals <= 0:
        return Decimal("1")
    return Decimal(f"1e-{max_decimals}")


def _coarser_quantum(a: Decimal, b: Decimal) -> Decimal:
    return a if a >= b else b


def format_hl_price(
    px: float | str | Decimal,
    sz_decimals: int,
    *,
    is_perp: bool = True,
    side: PriceSide | None = None,
) -> Decimal:
    """
    Format a Hyperliquid price deterministically using Decimal.

    Rules:
      - perps: max decimal places = 6 - sz_decimals
      - spot:  max decimal places = 8 - sz_decimals
      - integer prices are always allowed
      - no more than 5 significant figures unless the coarser integer rule wins

    Rounding policy:
      - buy:  round down / toward zero
      - sell: round up / away from zero
    """
    value = _to_decimal(px)
    if value <= 0:
        raise ValueError(f"price must be > 0 (got {value})")

    max_decimals = (MAX_DECIMALS_PERP if is_perp else MAX_DECIMALS_SPOT) - sz_decimals
    max_decimals = max(max_decimals, 0)
    sig_quantum = _quantum_for_sig_figs(value, sig_figs=5)
    decimal_quantum = _quantum_for_decimals(max_decimals)
    quantum = _coarser_quantum(sig_quantum, decimal_quantum)
    rounded = value.quantize(quantum, rounding=_rounding_for_price(side))
    return rounded.normalize() if rounded == rounded.to_integral() else rounded


def format_hl_qty(
    qty: float | str | Decimal,
    sz_decimals: int,
    *,
    rounding: QtyRounding = "down",
) -> Decimal:
    value = _to_decimal(qty)
    if value < 0:
        raise ValueError(f"qty must be non-negative (got {value})")
    quantum = _quantum_for_decimals(max(sz_decimals, 0))
    rounded = value.quantize(quantum, rounding=ROUND_DOWN if rounding == "down" else ROUND_UP)
    if abs(float(rounded)) <= EPSILON:
        return Decimal("0")
    return rounded.normalize() if rounded == rounded.to_integral() else rounded


def validate_hl_order_wire(
    px: Decimal,
    qty: Decimal,
    sz_decimals: int,
    *,
    is_perp: bool = True,
) -> None:
    if px <= 0:
        raise ValueError(f"formatted price must be > 0 (got {px})")
    if qty < 0:
        raise ValueError(f"formatted qty must be non-negative (got {qty})")

    max_decimals = (MAX_DECIMALS_PERP if is_perp else MAX_DECIMALS_SPOT) - sz_decimals
    max_decimals = max(max_decimals, 0)
    price_decimals = max(-px.as_tuple().exponent, 0)
    qty_decimals = max(-qty.as_tuple().exponent, 0)
    if price_decimals > max_decimals and px != px.to_integral():
        raise ValueError(
            f"formatted price has too many decimals ({price_decimals} > {max_decimals})"
        )
    if qty_decimals > sz_decimals:
        raise ValueError(
            f"formatted qty has too many decimals ({qty_decimals} > {sz_decimals})"
        )

    from hyperliquid.utils.signing import float_to_wire

    with localcontext() as ctx:
        ctx.prec = 28
        price_float = float(px)
        qty_float = float(qty)
    float_to_wire(price_float)
    float_to_wire(qty_float)


def decimal_to_wire_float(value: Decimal) -> float:
    """Convert an already-validated Decimal to a float safe for float_to_wire()."""
    return float(value)


def decimal_to_plain_string(value: Decimal) -> str:
    """Render a Decimal without scientific notation for logs/debug output."""
    return format(value, "f")
