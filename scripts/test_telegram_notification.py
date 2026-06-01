"""Send one safe Telegram health-check notification.

This script loads config through the normal runtime config path so `.env`
handling matches run_live_hl.py, but it never loads trading state, starts a
loop, or submits orders.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.config_loader import load_config
from src.notifications.telegram import TelegramNotifier, notify_event


def _run_id(config) -> str:  # noqa: ANN001
    return str(config.bot.get("run_id") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))


def _network(config) -> str:  # noqa: ANN001
    return str((config.hl or {}).get("network", config.bot.get("mode", "-")))


def build_health_check_event(config, config_path: str) -> dict:  # noqa: ANN001
    network = _network(config)
    coin = str((config.hl or {}).get("coin", config.symbol.get("ticker", "-")))
    return {
        "type": "safety",
        "event": "telegram_health_check",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": _run_id(config),
        "network": network,
        "coin": coin,
        "state": "health_check",
        "exchange_position_qty": "unknown",
        "local_position_qty": "unknown",
        "halted": False,
        "halt_reason": None,
        "reason": "operator_test_message",
        "config_path": config_path,
        "dry_run": True,
    }


def validate_health_check_config(config) -> str | None:  # noqa: ANN001
    network = _network(config)
    mode = str(config.bot.get("mode", ""))
    if network == "mainnet" or mode == "mainnet":
        return "refusing_mainnet_config"

    notifications = config.notifications or {}
    if not bool(notifications.get("enabled", False)):
        return "notifications_disabled"
    if not bool(notifications.get("telegram_enabled", False)):
        return "telegram_disabled"
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return "missing_TELEGRAM_BOT_TOKEN"
    if not os.environ.get("TELEGRAM_CHAT_ID"):
        return "missing_TELEGRAM_CHAT_ID"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one Telegram health-check notification")
    parser.add_argument("--config", required=True, help="Path to bot config YAML")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    blocker = validate_health_check_config(config)
    if blocker is not None:
        print(f"TELEGRAM_HEALTH_CHECK_BLOCKED: {blocker}")
        return 2

    notifier = TelegramNotifier.from_config(config.notifications or {})
    if not notifier.enabled:
        print("TELEGRAM_HEALTH_CHECK_BLOCKED: notifier_disabled")
        return 2

    event = build_health_check_event(config, args.config)
    sent = notify_event(notifier, event)
    if not sent:
        print("TELEGRAM_HEALTH_CHECK_FAILED: send_failed")
        return 1

    print("TELEGRAM_HEALTH_CHECK_SENT")
    print("network=" + event["network"])
    print("config_path=" + args.config)
    print("run_id=" + event["run_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
