"""Clear a false local_exchange_position_mismatch halt after recovery.

This operator utility is intentionally narrow:
  - no order placement
  - no cancellation
  - testnet only
  - only clears HALTED when local and exchange quantities already match
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.cancel_hl_order import clear_false_local_mismatch_halt
from src.core.config_loader import load_config
from src.hl.account import get_account_snapshot
from src.hl.client import HyperliquidClient
from src.reporting.state_writer import read_state, write_state


def main() -> int:
    parser = argparse.ArgumentParser(description="Clear proven false HL mismatch halt")
    parser.add_argument("--config", required=True)
    parser.add_argument("--coin", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--one-cycle", action="store_true")
    args = parser.parse_args()

    if not args.confirm or not args.one_cycle:
        print("ERROR: --confirm and --one-cycle are required")
        return 2

    config = load_config(args.config)
    hl_cfg = config.hl or {}
    network = str(hl_cfg.get("network", "testnet"))
    coin = str(hl_cfg.get("coin", config.symbol.get("ticker", args.coin)))
    if network != "testnet":
        print("ERROR: clear_false_hl_halt is testnet-only")
        return 2
    if args.coin != coin:
        print(f"ERROR: requested coin {args.coin} does not match config coin {coin}")
        return 2

    state_path = Path(config.logging.get("run_dir", "reports/btc_hl_testnet")) / "state.json"
    state = read_state(str(state_path))
    client = HyperliquidClient(network=network)
    snapshot = get_account_snapshot(
        hl_cfg.get("wallet_address", ""),
        client,
        include_spot=False,
        include_subaccounts=False,
        include_open_orders=True,
        collateral_mode=hl_cfg.get("collateral_mode", "unified"),
    )
    position = next((p for p in snapshot.positions if p.coin == coin), None)
    exchange_qty = position.qty if position is not None else 0.0

    cleared = clear_false_local_mismatch_halt(
        state,
        exchange_qty=exchange_qty,
        open_orders_count=snapshot.open_orders_count,
    )
    if not cleared:
        print("REFUSED: false_halt_conditions_not_met")
        print("state=" + state.state.value)
        print("halted=" + str(state.halted))
        print("halt_reason=" + str(state.halt_reason))
        print("local_qty=" + str(state.current_position_qty))
        print("exchange_qty=" + str(exchange_qty))
        print("open_orders_count=" + str(snapshot.open_orders_count))
        print("pending_order=" + str(state.pending_order.oid if state.pending_order else None))
        return 1

    write_state(state, str(state_path))
    print("CLEARED_FALSE_HALT")
    print("state=" + state.state.value)
    print("halted=" + str(state.halted))
    print("local_qty=" + str(state.current_position_qty))
    print("exchange_qty=" + str(exchange_qty))
    print("open_orders_count=" + str(snapshot.open_orders_count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
