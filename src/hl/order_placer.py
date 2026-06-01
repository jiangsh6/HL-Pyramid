"""
Hyperliquid order placement — Phase 4.

place_order(request, wallet_address, private_key, client) -> HLOrderResponse
cancel_order(coin, hl_oid, wallet_address, private_key, client) -> bool

SECURITY: private_key is NEVER logged, NEVER included in any error message.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests as _requests

from src.hl.client import HyperliquidClient
from src.hl.precision import (
    decimal_to_wire_float,
    decimal_to_plain_string,
    format_hl_price,
    format_hl_qty,
    validate_hl_order_wire,
)

_log = logging.getLogger(__name__)


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class HLOrderRequest:
    coin: str
    is_buy: bool
    sz: float
    limit_px: float
    order_type: Dict[str, Any]   # e.g. {"limit": {"tif": "Gtc"}}
    sz_decimals: int = 0
    min_size: float = 0.0
    reduce_only: bool = False
    cloid: Optional[str] = None  # UUID4 string from generate_cloid()


@dataclass
class HLOrderResponse:
    status: str                  # "ok" | "err"
    cloid: Optional[str] = None
    hl_oid: Optional[int] = None
    submitted_sz: float = 0.0
    submitted_limit_px: Optional[float] = None
    filled_sz: float = 0.0
    avg_fill_px: Optional[float] = None
    error: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def generate_cloid() -> str:
    """Return a UUID4 string to use as a client order ID."""
    return str(uuid.uuid4())


def _get_asset_index(coin: str, client: HyperliquidClient) -> int:
    """Fetch the integer asset index for a coin from the HL meta endpoint."""
    meta = client.post_info({"type": "meta"})
    universe = meta.get("universe", [])
    for i, asset in enumerate(universe):
        if asset.get("name") == coin:
            return i
    raise ValueError(f"coin_not_found_in_universe: {coin}")


def _parse_order_response(
    data: Any,
    cloid: Optional[str],
    *,
    submitted_sz: float = 0.0,
    submitted_limit_px: Optional[float] = None,
) -> HLOrderResponse:
    """Parse a raw /exchange response dict into an HLOrderResponse."""
    if data.get("status") != "ok":
        err = _sanitize_error(data.get("response", "unknown_exchange_error"))
        return HLOrderResponse(
            status="err",
            cloid=cloid,
            submitted_sz=submitted_sz,
            submitted_limit_px=submitted_limit_px,
            error=err,
        )

    resp_data = (
        data.get("response", {}).get("data", {})
        if isinstance(data.get("response"), dict)
        else {}
    )
    statuses = resp_data.get("statuses", [])
    if not statuses:
        return HLOrderResponse(
            status="ok",
            cloid=cloid,
            submitted_sz=submitted_sz,
            submitted_limit_px=submitted_limit_px,
        )

    first = statuses[0]
    if isinstance(first, dict):
        if "filled" in first:
            f = first["filled"]
            return HLOrderResponse(
                status="ok",
                cloid=cloid,
                hl_oid=f.get("oid"),
                submitted_sz=submitted_sz,
                submitted_limit_px=submitted_limit_px,
                filled_sz=float(f.get("totalSz", 0)),
                avg_fill_px=float(f.get("avgPx", 0)) if f.get("avgPx") else None,
            )
        if "resting" in first:
            r = first["resting"]
            return HLOrderResponse(
                status="ok",
                cloid=cloid,
                hl_oid=r.get("oid"),
                submitted_sz=submitted_sz,
                submitted_limit_px=submitted_limit_px,
            )
        parsed_error = _parse_rejection_status(first)
        if parsed_error is not None:
            return HLOrderResponse(
                status="err",
                cloid=cloid,
                submitted_sz=submitted_sz,
                submitted_limit_px=submitted_limit_px,
                error=parsed_error,
            )
    elif isinstance(first, str) and first != "success":
        return HLOrderResponse(
            status="err",
            cloid=cloid,
            submitted_sz=submitted_sz,
            submitted_limit_px=submitted_limit_px,
            error=_sanitize_error(first),
        )

    return HLOrderResponse(
        status="ok",
        cloid=cloid,
        submitted_sz=submitted_sz,
        submitted_limit_px=submitted_limit_px,
    )


def _sanitize_error(value: Any) -> str:
    text = str(value).replace("\n", " ").strip()
    text = re.sub(r"0x[a-fA-F0-9]{16,}", "[REDACTED_HEX]", text)
    if len(text) > 160:
        text = text[:160]
    return text or "unknown_exchange_error"


def _parse_rejection_status(status: Dict[str, Any]) -> Optional[str]:
    """
    Extract a structured rejection reason from an HL /exchange status entry.

    HL returns rejections under varied keys; this function picks them up and
    sanitizes the text. Examples of recognised keys (non-exhaustive):

      perpMarginRejected, minTradeNtlRejected, insufficientSpotBalanceRejected,
      oracleRejected, tickRejected, badAloPxRejected, postOnlyRejected,
      reduceOnlyRejected, error, rejected.

    Returns the sanitized reason string, or None if the status entry was not
    a recognised rejection (e.g. it was a 'filled' or 'resting' entry).
    """
    if "error" in status:
        return _sanitize_error(status["error"])
    if "rejected" in status:
        rejected = status["rejected"]
        if isinstance(rejected, dict):
            if "reason" in rejected:
                return _sanitize_error(rejected["reason"])
            if len(rejected) == 1:
                key, value = next(iter(rejected.items()))
                if value in ({}, None, ""):
                    return _sanitize_error(key)
                return _sanitize_error(f"{key}:{value}")
        return _sanitize_error(rejected)
    if len(status) == 1:
        key, value = next(iter(status.items()))
        if key not in {"filled", "resting"}:
            if isinstance(value, dict) and "reason" in value:
                return _sanitize_error(f"{key}:{value['reason']}")
            if value in ({}, None, ""):
                return _sanitize_error(key)
            return _sanitize_error(f"{key}:{value}")
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def place_order(
    request: HLOrderRequest,
    wallet_address: str,
    private_key: str,
    client: HyperliquidClient,
) -> HLOrderResponse:
    """
    Build, sign, and submit a limit order to Hyperliquid.

    Uses hyperliquid-python-sdk for EVM-compatible signing.
    SECURITY: private_key is never logged or included in any error string.
    """
    try:
        from hyperliquid.utils.signing import (
            order_request_to_order_wire,
            order_wires_to_order_action,
            get_timestamp_ms,
        )
        from src.hl.auth import build_signer

        asset_index = _get_asset_index(request.coin, client)
        wallet = build_signer(private_key)
        raw_limit_px = request.limit_px
        raw_qty = request.sz
        side = "buy" if request.is_buy else "sell"
        formatted_limit_px_dec = format_hl_price(
            raw_limit_px,
            request.sz_decimals,
            is_perp=True,
            side=side,
        )
        qty_rounding = "up" if request.reduce_only else "down"
        formatted_qty_dec = format_hl_qty(
            raw_qty,
            request.sz_decimals,
            rounding=qty_rounding,
        )
        if float(formatted_qty_dec) < float(request.min_size):
            return HLOrderResponse(
                status="err",
                cloid=request.cloid,
                submitted_sz=float(formatted_qty_dec),
                submitted_limit_px=float(formatted_limit_px_dec),
                error="qty_below_min_size_after_rounding",
            )
        validate_hl_order_wire(
            formatted_limit_px_dec,
            formatted_qty_dec,
            request.sz_decimals,
            is_perp=True,
        )
        formatted_limit_px = decimal_to_wire_float(formatted_limit_px_dec)
        formatted_qty = decimal_to_wire_float(formatted_qty_dec)
        _log.info(
            "HL order formatting coin=%s side=%s raw_limit_px=%s formatted_limit_px=%s "
            "raw_qty=%s formatted_qty=%s",
            request.coin,
            side,
            raw_limit_px,
            decimal_to_plain_string(formatted_limit_px_dec),
            raw_qty,
            decimal_to_plain_string(formatted_qty_dec),
        )

        order_req: Dict[str, Any] = {
            "coin": request.coin,
            "is_buy": request.is_buy,
            "sz": formatted_qty,
            "limit_px": formatted_limit_px,
            "order_type": request.order_type,
            "reduce_only": request.reduce_only,
        }
        # Attach cloid if provided — convert UUID4 to HL hex format
        if request.cloid is not None:
            from hyperliquid.utils.signing import Cloid
            cloid_hex = "0x" + request.cloid.replace("-", "")
            order_req["cloid"] = Cloid.from_str(cloid_hex)

        order_wire = order_request_to_order_wire(order_req, asset_index)
        order_action = order_wires_to_order_action([order_wire])

        from hyperliquid.utils.signing import sign_l1_action
        nonce = get_timestamp_ms()
        is_mainnet = getattr(client, "network", "testnet") == "mainnet"
        signature = sign_l1_action(wallet, order_action, None, nonce, None, is_mainnet)

        payload = {
            "action": order_action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        }

        resp = _requests.post(
            f"{client.base_url}/exchange",
            json=payload,
            timeout=client._timeout,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return _parse_order_response(
            resp.json(),
            request.cloid,
            submitted_sz=formatted_qty,
            submitted_limit_px=formatted_limit_px,
        )

    except _requests.exceptions.RequestException:
        # SECURITY: do not include exc details; they may contain URL or header fragments
        return HLOrderResponse(status="err", cloid=request.cloid, error="network_error")
    except Exception as exc:
        # SECURITY: sanitize exception text to avoid accidental secret leakage.
        err = f"order_placement_failed:{exc.__class__.__name__}"
        msg = _sanitize_error(str(exc))
        if msg and msg != "unknown_exchange_error":
            err = _sanitize_error(f"{err}:{msg}")
        return HLOrderResponse(status="err", cloid=request.cloid, error=err)


def cancel_order(
    coin: str,
    hl_oid: int,
    wallet_address: str,
    private_key: str,
    client: HyperliquidClient,
) -> bool:
    """
    Cancel an open order by its exchange order ID (oid).

    Returns True if cancelled, False if already filled or not found.
    SECURITY: private_key is never logged or included in any error string.
    """
    try:
        from hyperliquid.utils.signing import sign_l1_action, get_timestamp_ms
        from src.hl.auth import build_signer

        asset_index = _get_asset_index(coin, client)
        wallet = build_signer(private_key)

        cancel_action = {
            "type": "cancel",
            "cancels": [{"a": asset_index, "o": hl_oid}],
        }
        nonce = get_timestamp_ms()
        is_mainnet = getattr(client, "network", "testnet") == "mainnet"
        signature = sign_l1_action(wallet, cancel_action, None, nonce, None, is_mainnet)

        payload = {
            "action": cancel_action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        }

        resp = _requests.post(
            f"{client.base_url}/exchange",
            json=payload,
            timeout=client._timeout,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "ok":
            return False
        resp_data = (
            data.get("response", {}).get("data", {})
            if isinstance(data.get("response"), dict)
            else {}
        )
        statuses = resp_data.get("statuses", [])
        return bool(statuses and statuses[0] == "success")

    except Exception:
        return False
