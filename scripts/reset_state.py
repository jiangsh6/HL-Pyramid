#!/usr/bin/env python
"""
Manual state reset (HALTED or EXITED → FLAT).

REQUIRES --confirm.  Without it, the script refuses and exits with status 2.
Section 4.2 / 7.3 / 23.13.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running this script directly without an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config_loader import load_config
from src.core.models import EPSILON
from src.hl.account import get_account_snapshot
from src.hl.client import HyperliquidClient
from src.reporting.state_writer import read_state, reset_state_to_flat, write_state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--confirm", action="store_true",
        help="REQUIRED.  Without this flag the script refuses to run.",
    )
    args = parser.parse_args(argv)

    if not args.confirm:
        print(
            "ERROR: --confirm is required.  This operation clears the position, "
            "lots, PnL counters, and halt state.  Re-run with --confirm to proceed.",
            file=sys.stderr,
        )
        return 2

    cfg = load_config(args.config)
    state_path = Path(cfg.logging["run_dir"]) / "state.json"
    if not state_path.exists():
        print(f"No state.json at {state_path}", file=sys.stderr)
        return 1

    state = read_state(str(state_path))
    if state.current_position_qty > EPSILON or state.pending_order is not None:
        print(
            "ERROR: refusing reset while local state still has a position or "
            "pending order. Reconcile/flatten/cancel first.",
            file=sys.stderr,
        )
        return 1
    if cfg.hl and cfg.bot.get("mode") in {"testnet", "mainnet"}:
        hl_cfg = cfg.hl
        client = HyperliquidClient(network=hl_cfg.get("network", "testnet"))
        snapshot = get_account_snapshot(
            hl_cfg.get("wallet_address", ""),
            client,
            include_open_orders=True,
            collateral_mode=hl_cfg.get("collateral_mode", "unified"),
        )
        coin = hl_cfg.get("coin", cfg.symbol.get("ticker", state.symbol))
        exchange_pos = next((pos for pos in snapshot.positions if pos.coin == coin), None)
        exchange_qty = exchange_pos.qty if exchange_pos is not None else 0.0
        if exchange_qty > EPSILON or snapshot.open_orders_count > 0:
            print(
                "ERROR: refusing reset while exchange still has a position or open order.",
                file=sys.stderr,
            )
            return 1
    prev = state.state.value
    reset_state_to_flat(state)
    write_state(state, str(state_path))
    print(f"State reset: {prev} → FLAT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
