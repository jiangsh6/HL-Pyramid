"""Read-only Hyperliquid account diagnostic.

Prints unified-collateral diagnostics so an operator can see, in one place,
whether perp trading is currently fundable under the configured collateral
mode. NEVER prints raw private keys or full signed payloads.

Usage:
    python scripts/diagnose_hl_account.py --config config/btc_testnet_order_probe.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.config_loader import load_config
from src.hl.account import get_account_snapshot
from src.hl.auth import build_signer, get_private_key
from src.hl.client import HyperliquidClient


def _mask(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr
    return addr[:6] + "..." + addr[-3:]


def _print_endpoint_summary(name: str, resp) -> None:
    print(f"--- {name} ---")
    if isinstance(resp, dict):
        print("kind=dict")
        print("top_keys=" + ",".join(sorted(resp.keys())[:20]))
        margin = resp.get("marginSummary") if isinstance(resp.get("marginSummary"), dict) else {}
        cross = resp.get("crossMarginSummary") if isinstance(resp.get("crossMarginSummary"), dict) else {}
        print("margin_account_value=" + str(margin.get("accountValue")))
        print("cross_account_value=" + str(cross.get("accountValue")))
        print("withdrawable=" + str(resp.get("withdrawable", margin.get("withdrawable"))))
        asset_positions = resp.get("assetPositions", [])
        if isinstance(asset_positions, list):
            print("asset_positions_count=" + str(len(asset_positions)))
        balances = resp.get("balances", [])
        if isinstance(balances, list):
            items = []
            for b in balances[:10]:
                if isinstance(b, dict):
                    coin = b.get("coin") or b.get("token") or b.get("name")
                    total = b.get("total") or b.get("balance") or b.get("totalRaw")
                    hold = b.get("hold") or b.get("locked") or b.get("holdRaw")
                    entry = b.get("entryNtl") or b.get("entryNotional")
                    items.append(f"{coin}:{total}:hold={hold}:entryNtl={entry}")
            print("balances=" + "|".join(items))
    elif isinstance(resp, list):
        print("kind=list")
        print("count=" + str(len(resp)))
    else:
        print("kind=" + type(resp).__name__)
        print("value=" + str(resp)[:200])


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Hyperliquid account diagnostic")
    parser.add_argument("--config", default="config/btc_testnet_order_probe.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    hl_cfg = cfg.hl or {}
    network = hl_cfg.get("network", "testnet")
    wallet = hl_cfg.get("wallet_address", "")
    collateral_mode = hl_cfg.get("collateral_mode", "unified")
    key = get_private_key(cfg)
    agent_address = build_signer(key).address
    client = HyperliquidClient(network=network)

    print("network=" + network)
    print("account_address=" + _mask(wallet))
    print("agent_wallet_address=" + _mask(agent_address))
    print("same_address=" + str(wallet.lower() == agent_address.lower()))
    print("collateral_mode=" + collateral_mode)

    snapshot = get_account_snapshot(
        wallet, client,
        include_spot=True,
        include_subaccounts=True,
        include_open_orders=True,
        collateral_mode=collateral_mode,
    )

    print("--- snapshot ---")
    print("perp_account_value=" + str(snapshot.perp_account_value))
    print("perp_withdrawable=" + str(snapshot.perp_withdrawable))
    print("perp_positions=" + str(len(snapshot.perp_positions)))
    print("spot_usdc_total=" + str(snapshot.spot_usdc_total))
    print("spot_usdc_hold=" + str(snapshot.spot_usdc_hold))
    print("spot_usdc_available=" + str(snapshot.spot_usdc_available))
    print("effective_trading_collateral=" + str(snapshot.effective_trading_collateral))
    print("trading_collateral_available=" + str(snapshot.trading_collateral_available))
    print("perp_collateral_available=" + str(snapshot.perp_collateral_available))
    print("collateral_reason=" + str(snapshot.collateral_reason))
    print("subaccounts=" + str(len(snapshot.subaccounts)))
    if snapshot.subaccounts:
        pieces = []
        for sub in snapshot.subaccounts[:10]:
            pieces.append(
                f"{sub.name or '-'}:{_mask(sub.address)}:"
                f"perp={sub.perp_account_value}:spot_usdc={sub.spot_usdc_total}"
            )
        print("subaccount_summary=" + "|".join(pieces))
    print("open_orders_count=" + str(snapshot.open_orders_count))

    # ── Mode-aware operator hints ───────────────────────────────────────
    perp_zero = (snapshot.perp_account_value or 0) <= 0 and (snapshot.perp_withdrawable or 0) <= 0
    if collateral_mode == "unified" and perp_zero and snapshot.spot_usdc_available > 0:
        print(
            "INFO: Unified collateral detected via spot USDC. Perp clearinghouse "
            "accountValue is 0, but trading collateral is available under unified "
            "collateral mode."
        )
    elif collateral_mode == "clearinghouse_only" and perp_zero and snapshot.spot_usdc_available > 0:
        print(
            "WARN: Spot USDC detected but ignored because collateral_mode="
            "clearinghouse_only. Switch hl.collateral_mode to 'unified' to allow "
            "spot USDC as perp collateral, or move funds into the perp clearinghouse."
        )
    elif not snapshot.trading_collateral_available:
        print(
            "WARN: No trading collateral available. Perp orders will be blocked "
            "until effective_trading_collateral > 0."
        )

    queries = [
        ("clearinghouseState", {"type": "clearinghouseState", "user": wallet}),
        ("spotClearinghouseState", {"type": "spotClearinghouseState", "user": wallet}),
        ("subAccounts", {"type": "subAccounts", "user": wallet}),
        ("openOrders", {"type": "openOrders", "user": wallet}),
        ("userFills", {"type": "userFills", "user": wallet}),
    ]
    for name, payload in queries:
        try:
            resp = client.post_info(payload)
            _print_endpoint_summary(name, resp)
        except Exception as exc:  # noqa: BLE001
            print(f"--- {name} ---")
            print("error=" + str(exc)[:300])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
