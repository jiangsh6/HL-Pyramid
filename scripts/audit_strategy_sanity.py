"""Generate a strategy sanity audit for a supervised testnet soak config.

This script does not load trading state, start loops, or submit orders.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.config_loader import load_config
from src.notifications.telegram import TelegramNotifier, notify_event
from src.reporting.strategy_sanity_audit import (
    build_strategy_sanity_audit,
    load_latest_signal,
    strategy_audit_event,
    write_strategy_sanity_audit,
)


def _network(config) -> str:  # noqa: ANN001
    return str((config.hl or {}).get("network", config.bot.get("mode", "-")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate strategy sanity audit")
    parser.add_argument("--config", required=True, help="Path to bot config YAML")
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Write the audit only; do not attempt Telegram notification",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if _network(config) == "mainnet":
        print("STRATEGY_SANITY_AUDIT_BLOCKED: refusing_mainnet_config")
        return 2

    latest_signal = load_latest_signal(config)
    audit = build_strategy_sanity_audit(config, latest_signal)
    report_path = write_strategy_sanity_audit(config, audit)

    print("STRATEGY_SANITY_AUDIT_WRITTEN")
    print("report_path=" + str(report_path))
    print("parameters_reviewed=" + str(audit["parameters_reviewed"]))
    print("warning_count=" + str(audit["warning_count"]))
    for row in list(audit.get("warnings") or [])[:10]:
        print(
            "warning="
            + str(row["parameter"])
            + " assessment="
            + str(row["assessment"])
            + " value="
            + str(row["interpreted_value"])
        )

    if not args.no_telegram:
        notifier = TelegramNotifier.from_config(config.notifications or {})
        if notifier.enabled:
            sent = notify_event(notifier, strategy_audit_event(config, audit, report_path))
            print("telegram_sent=" + str(bool(sent)).lower())
        else:
            print("telegram_sent=false reason=notifier_disabled")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
