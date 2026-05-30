"""
Hyperliquid order placement — Phase 4.

place_order(request, wallet_address, private_key, client) -> HLOrderResponse
cancel_order(coin, hl_oid, wallet_address, private_key, client) -> bool

SECURITY: private_key is NEVER logged, NEVER included in any error message.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests as _requests

from src.hl.client import HyperliquidClient


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class HLOrderRequest:
    coin: str
    is_buy: bool
    sz: float
    limit_px: float
    order_type: Dict[str, Any]   # e.g. {"limit": {"tif": "Gtc"}}
    reduce_only: bool = False
    cloid: Optional[str] = None  # UUID4 string from generate_cloid()


@dataclass
class HLOrderResponse:
    status: str                  # "ok" | "err"
    cloid: Optional[str] = None
    hl_oid: Optional[int] = None
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


def _parse_order_response(data: Any, cloid: Optional[str]) -> HLOrderResponse:
    """Parse a raw /exchange response dict into an HLOrderResponse."""
    if data.get("status") != "ok":
        err = str(data.get("response", "unknown_exchange_error"))
        return HLOrderResponse(status="err", cloid=cloid, error=err)

    resp_data = (
        data.get("response", {}).get("data", {})
        if isinstance(data.get("response"), dict)
        else {}
    )
    statuses = resp_data.get("statuses", [])
    if not statuses:
        return HLOrderResponse(status="ok", cloid=cloid)

    first = statuses[0]
    if isinstance(first, dict):
        if "filled" in first:
            f = first["filled"]
            return HLOrderResponse(
                status="ok",
                cloid=cloid,
                hl_oid=f.get("oid"),
                filled_sz=float(f.get("totalSz", 0)),
                avg_fill_px=float(f.get("avgPx", 0)) if f.get("avgPx") else None,
            )
        if "resting" in first:
            r = first["resting"]
            return HLOrderResponse(status="ok", cloid=cloid, hl_oid=r.get("oid"))

    return HLOrderResponse(status="ok", cloid=cloid)


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

        order_req: Dict[str, Any] = {
            "coin": request.coin,
            "is_buy": request.is_buy,
            "sz": request.sz,
            "limit_px": request.limit_px,
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
        signature = sign_l1_action(wallet, order_action, None, nonce, None, False)

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
        return _parse_order_response(resp.json(), request.cloid)

    except _requests.exceptions.RequestException:
        # SECURITY: do not include exc details; they may contain URL or header fragments
        return HLOrderResponse(status="err", cloid=request.cloid, error="network_error")
    except Exception:
        # SECURITY: do not include exc details; protect against accidental key exposure
        return HLOrderResponse(status="err", cloid=request.cloid, error="order_placement_failed")


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
        signature = sign_l1_action(wallet, cancel_action, None, nonce, None, False)

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
