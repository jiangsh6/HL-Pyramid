"""
Hyperliquid account state — Phase 3 / unified-collateral refactor.

get_account_snapshot(wallet_address, client, ..., collateral_mode="unified")
    -> HLAccountSnapshot

The snapshot exposes two distinct views:

* Raw perp/spot diagnostics  — never combined, never altered to match policy.
* Effective trading collateral — derived from raw fields and the active
  `collateral_mode`. This is the ONLY field that order-gating code should
  consume.

Collateral rules:

  collateral_mode == "clearinghouse_only":
      effective_trading_collateral = max(perp_withdrawable, perp_account_value)
      trading_collateral_available = effective_trading_collateral > 0
      Spot USDC is ignored.

  collateral_mode == "unified":
      effective_trading_collateral = max(perp_withdrawable,
                                         perp_account_value,
                                         spot_usdc_available)
      trading_collateral_available = effective_trading_collateral > 0
      Sources are taken with max(...) — never summed — to avoid double-count.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


# Public constants for callers (gating code, diagnostics, tests).
COLLATERAL_MODE_UNIFIED = "unified"
COLLATERAL_MODE_CLEARINGHOUSE_ONLY = "clearinghouse_only"
_VALID_COLLATERAL_MODES = (COLLATERAL_MODE_UNIFIED, COLLATERAL_MODE_CLEARINGHOUSE_ONLY)


class HLAPIError(Exception):
    """Raised when the HL API returns an unexpected or malformed response."""


@dataclass
class HLPosition:
    coin: str
    qty: float                # positive = long, negative = short
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    liquidation_price: Optional[float]
    margin_used: float
    leverage: float


@dataclass
class HLSpotBalance:
    coin: str
    total: float
    hold: float
    entry_ntl: float


@dataclass
class HLSubaccountSummary:
    name: Optional[str]
    address: str
    perp_account_value: float = 0.0
    spot_usdc_total: float = 0.0


@dataclass
class HLOpenOrder:
    oid: Optional[int]
    coin: str
    side: str
    qty: float
    limit_px: float
    status: str = "open"


@dataclass
class HLFill:
    coin: str
    side: str
    qty: float
    price: float
    timestamp_ms: Optional[int] = None
    oid: Optional[int] = None
    direction: Optional[str] = None


@dataclass
class HLAccountSnapshot:
    """
    Account-level snapshot used by reconciler, risk gates, and diagnostics.

    Raw perp fields stay separate from raw spot fields — callers must use
    `effective_trading_collateral` / `trading_collateral_available` rather
    than mixing the raw values themselves.
    """

    # ── Raw perp clearinghouse fields ────────────────────────────────────
    account_value: float
    margin_used: float
    withdrawable: float
    positions: List[HLPosition] = field(default_factory=list)
    perp_account_value: Optional[float] = None
    perp_margin_used: Optional[float] = None
    perp_withdrawable: Optional[float] = None
    perp_positions: List[HLPosition] = field(default_factory=list)

    # ── Raw spot fields ──────────────────────────────────────────────────
    spot_balances: List[HLSpotBalance] = field(default_factory=list)
    spot_usdc_total: float = 0.0
    spot_usdc_hold: float = 0.0
    spot_usdc_available: float = 0.0

    # ── Subaccount summary (when queried) ────────────────────────────────
    subaccounts: List[HLSubaccountSummary] = field(default_factory=list)
    open_orders_count: int = 0
    open_orders: List[HLOpenOrder] = field(default_factory=list)

    # ── Effective trading collateral (derived, mode-aware) ───────────────
    collateral_mode: str = COLLATERAL_MODE_UNIFIED
    effective_trading_collateral: float = 0.0
    trading_collateral_available: bool = False
    collateral_reason: Optional[str] = None

    # Legacy field kept so existing callers/tests continue to compile. New
    # code should consume `trading_collateral_available`. Under unified mode
    # this still reflects only perp clearinghouse availability so callers
    # that need the narrow perp answer can still get it.
    perp_collateral_available: bool = False

    def __post_init__(self) -> None:
        if self.perp_account_value is None:
            self.perp_account_value = self.account_value
        if self.perp_margin_used is None:
            self.perp_margin_used = self.margin_used
        if self.perp_withdrawable is None:
            self.perp_withdrawable = self.withdrawable
        if not self.perp_positions and self.positions:
            self.perp_positions = list(self.positions)
        if not self.positions and self.perp_positions:
            self.positions = list(self.perp_positions)


def _compute_collateral(
    mode: str,
    perp_account_value: float,
    perp_withdrawable: float,
    spot_usdc_available: float,
) -> tuple[float, bool, Optional[str]]:
    """
    Apply mode-specific collateral rules.

    Returns (effective, available, reason). Uses max(...) across sources,
    never sum, to prevent double-counting under unified collateral.
    """
    perp_available = perp_account_value > 0 or perp_withdrawable > 0

    if mode == COLLATERAL_MODE_CLEARINGHOUSE_ONLY:
        effective = max(perp_account_value, perp_withdrawable)
        if effective > 0:
            return effective, True, "perp_collateral_available"
        if spot_usdc_available > 0:
            return 0.0, False, "spot_balance_not_perp_margin"
        return 0.0, False, "no_perp_collateral"

    # default: unified
    effective = max(perp_account_value, perp_withdrawable, spot_usdc_available)
    if effective <= 0:
        return 0.0, False, "no_unified_collateral_available"
    if perp_available and effective == max(perp_account_value, perp_withdrawable):
        return effective, True, "unified_perp_account_value_available"
    return effective, True, "unified_spot_usdc_available"


def _parse_positions(asset_positions: Any) -> List[HLPosition]:
    if not isinstance(asset_positions, list):
        raise HLAPIError("'assetPositions' is not a list")

    positions: List[HLPosition] = []
    for entry in asset_positions:
        pos = entry.get("position") if isinstance(entry, dict) else entry
        if not isinstance(pos, dict):
            raise HLAPIError(f"Unexpected position entry format: {entry}")

        try:
            coin = str(pos["coin"])
            qty = float(pos["szi"])
            entry_price = float(pos.get("entryPx", 0) or 0)
            unrealized_pnl = float(pos.get("unrealizedPnl", 0) or 0)
            margin_used_pos = float(pos.get("marginUsed", 0) or 0)

            liq_px_raw = pos.get("liquidationPx")
            liq_px: Optional[float] = float(liq_px_raw) if liq_px_raw is not None else None

            lev_raw = pos.get("leverage", 1)
            leverage = float(lev_raw.get("value", 1)) if isinstance(lev_raw, dict) else float(lev_raw)

            mark_px_raw = pos.get("markPx")
            if mark_px_raw is not None:
                mark_price = float(mark_px_raw)
            elif abs(qty) > 0:
                pos_val = float(pos.get("positionValue", 0) or 0)
                mark_price = abs(pos_val) / abs(qty)
            else:
                mark_price = entry_price

        except (KeyError, TypeError, ValueError) as exc:
            raise HLAPIError(f"Failed to parse position entry: {exc}") from exc

        positions.append(HLPosition(
            coin=coin,
            qty=qty,
            entry_price=entry_price,
            mark_price=mark_price,
            unrealized_pnl=unrealized_pnl,
            liquidation_price=liq_px,
            margin_used=margin_used_pos,
            leverage=leverage,
        ))
    return positions


def _parse_spot_balances(resp: Any) -> List[HLSpotBalance]:
    if not isinstance(resp, dict):
        return []
    balances = resp.get("balances", [])
    if not isinstance(balances, list):
        return []

    parsed: List[HLSpotBalance] = []
    for item in balances:
        if not isinstance(item, dict):
            continue
        try:
            parsed.append(HLSpotBalance(
                coin=str(item.get("coin") or item.get("token") or item.get("name") or ""),
                total=float(item.get("total", 0) or 0),
                hold=float(item.get("hold", 0) or 0),
                entry_ntl=float(item.get("entryNtl", 0) or 0),
            ))
        except (TypeError, ValueError):
            continue
    return parsed


def _parse_subaccounts(resp: Any) -> List[HLSubaccountSummary]:
    if resp is None:
        return []
    if isinstance(resp, dict):
        entries = resp.get("subAccounts") or resp.get("subaccounts") or []
    elif isinstance(resp, list):
        entries = resp
    else:
        return []

    parsed: List[HLSubaccountSummary] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        address = str(item.get("address") or item.get("user") or item.get("subAccountUser") or "")
        if not address:
            continue
        perp_value = 0.0
        margin = item.get("marginSummary")
        if isinstance(margin, dict):
            try:
                perp_value = float(margin.get("accountValue", 0) or 0)
            except (TypeError, ValueError):
                perp_value = 0.0
        spot_usdc = 0.0
        balances = item.get("balances")
        if isinstance(balances, list):
            for bal in balances:
                if isinstance(bal, dict) and str(bal.get("coin")) == "USDC":
                    try:
                        spot_usdc = float(bal.get("total", 0) or 0)
                    except (TypeError, ValueError):
                        spot_usdc = 0.0
                    break
        parsed.append(HLSubaccountSummary(
            name=item.get("name"),
            address=address,
            perp_account_value=perp_value,
            spot_usdc_total=spot_usdc,
        ))
    return parsed


def _parse_open_orders(resp: Any) -> List[HLOpenOrder]:
    if not isinstance(resp, list):
        return []
    parsed: List[HLOpenOrder] = []
    for item in resp:
        if not isinstance(item, dict):
            continue
        oid = item.get("oid")
        order = item.get("order") if isinstance(item.get("order"), dict) else item
        try:
            coin = str(order.get("coin") or item.get("coin") or "")
            side = str(order.get("side") or item.get("side") or "")
            qty = float(order.get("sz") or item.get("sz") or 0)
            limit_px = float(
                order.get("limitPx")
                or order.get("px")
                or item.get("limitPx")
                or item.get("px")
                or 0
            )
        except (TypeError, ValueError):
            continue
        parsed.append(HLOpenOrder(
            oid=int(oid) if oid is not None else None,
            coin=coin,
            side=side,
            qty=qty,
            limit_px=limit_px,
            status="open",
        ))
    return parsed


def _parse_user_fills(resp: Any) -> List[HLFill]:
    if not isinstance(resp, list):
        return []
    parsed: List[HLFill] = []
    for item in resp:
        if not isinstance(item, dict):
            continue
        try:
            coin = str(item.get("coin") or "")
            side = str(item.get("side") or item.get("dir") or "")
            direction = str(item.get("dir") or "") or None
            qty = float(item.get("sz") or item.get("qty") or item.get("size") or 0)
            price = float(item.get("px") or item.get("price") or 0)
            raw_time = item.get("time") or item.get("timestamp")
            timestamp_ms = int(raw_time) if raw_time is not None else None
            raw_oid = item.get("oid")
            oid = int(raw_oid) if raw_oid is not None else None
        except (TypeError, ValueError):
            continue
        parsed.append(HLFill(
            coin=coin,
            side=side,
            qty=qty,
            price=price,
            timestamp_ms=timestamp_ms,
            oid=oid,
            direction=direction,
        ))
    return parsed


def get_recent_user_fills(wallet_address: str, client: Any) -> List[HLFill]:
    """Fetch recent user fills from Hyperliquid /info without logging raw payloads."""
    resp = client.post_info({"type": "userFills", "user": wallet_address})
    return _parse_user_fills(resp)


def get_account_snapshot(
    wallet_address: str,
    client: Any,
    *,
    include_spot: bool = True,
    include_subaccounts: bool = False,
    include_open_orders: bool = False,
    collateral_mode: str = COLLATERAL_MODE_UNIFIED,
) -> HLAccountSnapshot:
    """
    POST /info with type="clearinghouseState" and parse the response.

    Returns HLAccountSnapshot. Spot balances are parsed and exposed as raw
    diagnostics; whether they count toward `effective_trading_collateral`
    depends on `collateral_mode` (default "unified" — match current HL).
    """
    if collateral_mode not in _VALID_COLLATERAL_MODES:
        raise ValueError(
            f"collateral_mode must be one of {_VALID_COLLATERAL_MODES} "
            f"(got '{collateral_mode}')"
        )

    payload = {"type": "clearinghouseState", "user": wallet_address}

    try:
        resp = client.post_info(payload)
    except Exception as exc:
        raise HLAPIError(f"Failed to fetch clearinghouseState: {exc}") from exc

    if not isinstance(resp, dict):
        raise HLAPIError(f"Expected dict response, got {type(resp).__name__}")

    margin_summary = resp.get("marginSummary")
    if not isinstance(margin_summary, dict):
        raise HLAPIError("Response missing 'marginSummary' dict")

    try:
        account_value = float(margin_summary.get("accountValue", 0) or 0)
        margin_used = float(margin_summary.get("totalMarginUsed", 0) or 0)
        withdrawable = float(resp.get("withdrawable", margin_summary.get("withdrawable", 0)) or 0)
    except (TypeError, ValueError) as exc:
        raise HLAPIError(f"Failed to parse marginSummary fields: {exc}") from exc

    positions = _parse_positions(resp.get("assetPositions", []))

    spot_balances: List[HLSpotBalance] = []
    if include_spot:
        try:
            spot_resp = client.post_info({"type": "spotClearinghouseState", "user": wallet_address})
            spot_balances = _parse_spot_balances(spot_resp)
        except Exception:
            spot_balances = []

    subaccounts: List[HLSubaccountSummary] = []
    if include_subaccounts:
        try:
            sub_resp = client.post_info({"type": "subAccounts", "user": wallet_address})
            subaccounts = _parse_subaccounts(sub_resp)
        except Exception:
            subaccounts = []

    open_orders: List[HLOpenOrder] = []
    open_orders_count = 0
    if include_open_orders:
        try:
            open_orders_resp = client.post_info({"type": "openOrders", "user": wallet_address})
            open_orders = _parse_open_orders(open_orders_resp)
            open_orders_count = len(open_orders)
        except Exception:
            open_orders = []
            open_orders_count = 0

    spot_usdc_total = 0.0
    spot_usdc_hold = 0.0
    for balance in spot_balances:
        if balance.coin == "USDC":
            spot_usdc_total = balance.total
            spot_usdc_hold = balance.hold
            break
    spot_usdc_available = max(spot_usdc_total - spot_usdc_hold, 0.0)

    effective_collateral, collateral_available, collateral_reason = _compute_collateral(
        mode=collateral_mode,
        perp_account_value=account_value,
        perp_withdrawable=withdrawable,
        spot_usdc_available=spot_usdc_available,
    )

    perp_collateral_available = account_value > 0 or withdrawable > 0

    return HLAccountSnapshot(
        account_value=account_value,
        margin_used=margin_used,
        withdrawable=withdrawable,
        positions=positions,
        perp_account_value=account_value,
        perp_margin_used=margin_used,
        perp_withdrawable=withdrawable,
        perp_positions=positions,
        spot_balances=spot_balances,
        spot_usdc_total=spot_usdc_total,
        spot_usdc_hold=spot_usdc_hold,
        spot_usdc_available=spot_usdc_available,
        subaccounts=subaccounts,
        open_orders_count=open_orders_count,
        open_orders=open_orders,
        collateral_mode=collateral_mode,
        effective_trading_collateral=effective_collateral,
        trading_collateral_available=collateral_available,
        collateral_reason=collateral_reason,
        perp_collateral_available=perp_collateral_available,
    )
