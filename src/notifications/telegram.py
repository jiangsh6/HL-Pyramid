from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import requests

_log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.8g}"
    return str(value)


def _semantic_value(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:.8g}"
    return str(value)


def alert_toggle_enabled(config: Mapping[str, Any], key: str) -> bool:
    if not bool(config.get("enabled", False)):
        return False
    if not bool(config.get("telegram_enabled", False)):
        return False
    return bool(config.get(key, False))


@dataclass
class TelegramNotifier:
    enabled: bool
    token: Optional[str] = None
    chat_id: Optional[str] = None
    timeout_seconds: float = 5.0
    retry_count: int = 0
    retry_delay_seconds: float = 0.0
    logger: logging.Logger = _log

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        logger: Optional[logging.Logger] = None,
    ) -> "TelegramNotifier":
        log = logger or _log
        configured = bool(config.get("enabled", False)) and bool(config.get("telegram_enabled", False))
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        timeout = float(config.get("telegram_timeout_seconds", 5.0) or 5.0)
        retry_count = max(0, int(config.get("retry_count", 0) or 0))
        retry_delay = max(0.0, float(config.get("retry_delay_seconds", 0.0) or 0.0))
        if not configured:
            return cls(
                enabled=False,
                timeout_seconds=timeout,
                retry_count=retry_count,
                retry_delay_seconds=retry_delay,
                logger=log,
            )
        if not token or not chat_id:
            log.info("alert_disabled=true reason=missing_telegram_env")
            return cls(
                enabled=False,
                timeout_seconds=timeout,
                retry_count=retry_count,
                retry_delay_seconds=retry_delay,
                logger=log,
            )
        return cls(
            enabled=True,
            token=token,
            chat_id=chat_id,
            timeout_seconds=timeout,
            retry_count=retry_count,
            retry_delay_seconds=retry_delay,
            logger=log,
        )

    def send_message(self, text: str) -> bool:
        if not self.enabled:
            return False
        if not self.token or not self.chat_id:
            self.logger.info("alert_disabled=true reason=missing_telegram_env")
            return False
        max_attempts = self.retry_count + 1
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                return True
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "telegram_send_failed=true attempt=%s max_attempts=%s error_type=%s",
                    attempt,
                    max_attempts,
                    type(exc).__name__,
                )
                if attempt < max_attempts and self.retry_delay_seconds > 0:
                    time.sleep(self.retry_delay_seconds)
        return False


def format_event(event: Mapping[str, Any]) -> str:
    event_type = str(event.get("type") or "event")
    timestamp = _value(event.get("timestamp") or _now_iso())

    if event_type == "heartbeat":
        return "\n".join([
            "HL heartbeat",
            f"timestamp={timestamp}",
            f"run_id={_value(event.get('run_id'))}",
            f"network={_value(event.get('network'))}",
            f"coin={_value(event.get('coin'))}",
            f"state={_value(event.get('state'))}",
            f"exchange_position_qty={_value(event.get('exchange_position_qty'))}",
            f"local_position_qty={_value(event.get('local_position_qty'))}",
            f"open_orders_count={_value(event.get('open_orders_count'))}",
            f"pending_order={_value(event.get('pending_order_status'))}/{_value(event.get('pending_order_oid'))}",
            f"halted={_value(event.get('halted'))}",
            f"halt_reason={_value(event.get('halt_reason'))}",
            f"last_decision={_value(event.get('last_decision'))}",
            f"dry_run={_value(event.get('dry_run'))}",
        ])

    if event_type == "decision":
        return "\n".join([
            "HL decision",
            f"timestamp={timestamp}",
            f"run_id={_value(event.get('run_id'))}",
            f"network={_value(event.get('network'))}",
            f"coin={_value(event.get('coin'))}",
            f"state={_value(event.get('state'))}",
            f"exchange_position_qty={_value(event.get('exchange_position_qty'))}",
            f"local_position_qty={_value(event.get('local_position_qty'))}",
            f"decision={_value(event.get('decision'))}",
            f"reason={_value(event.get('reason'))}",
            f"state_before={_value(event.get('state_before'))}",
            f"price={_value(event.get('price'))}",
            f"mark={_value(event.get('mark'))}",
            f"oracle={_value(event.get('oracle'))}",
            f"dry_run={_value(event.get('dry_run'))}",
        ])

    if event_type == "order":
        return "\n".join([
            "HL order",
            f"timestamp={timestamp}",
            f"run_id={_value(event.get('run_id'))}",
            f"network={_value(event.get('network'))}",
            f"coin={_value(event.get('coin'))}",
            f"state={_value(event.get('state'))}",
            f"exchange_position_qty={_value(event.get('exchange_position_qty'))}",
            f"local_position_qty={_value(event.get('local_position_qty'))}",
            f"status={_value(event.get('status'))}",
            f"action={_value(event.get('action'))}",
            f"reduce_only={_value(event.get('reduce_only'))}",
            f"side={_value(event.get('side'))}",
            f"qty={_value(event.get('qty'))}",
            f"px={_value(event.get('px'))}",
            f"oid={_value(event.get('oid'))}",
            f"filled_qty={_value(event.get('filled_qty'))}",
            f"remaining_qty={_value(event.get('remaining_qty'))}",
        ])

    if event_type == "safety":
        return "\n".join([
            "HL safety",
            f"timestamp={timestamp}",
            f"run_id={_value(event.get('run_id'))}",
            f"network={_value(event.get('network'))}",
            f"event={_value(event.get('event'))}",
            f"coin={_value(event.get('coin'))}",
            f"state={_value(event.get('state'))}",
            f"exchange_position_qty={_value(event.get('exchange_position_qty'))}",
            f"local_position_qty={_value(event.get('local_position_qty'))}",
            f"halted={_value(event.get('halted'))}",
            f"halt_reason={_value(event.get('halt_reason'))}",
            f"reason={_value(event.get('reason'))}",
            f"config_path={_value(event.get('config_path'))}",
            f"dry_run={_value(event.get('dry_run'))}",
        ])

    if event_type == "summary":
        return "\n".join([
            "HL summary",
            f"timestamp={timestamp}",
            f"run_id={_value(event.get('run_id'))}",
            f"network={_value(event.get('network'))}",
            f"coin={_value(event.get('coin'))}",
            f"state={_value(event.get('state'))}",
            f"exchange_position_qty={_value(event.get('exchange_position_qty'))}",
            f"local_position_qty={_value(event.get('local_position_qty'))}",
            f"summary={_value(event.get('summary'))}",
        ])

    if event_type == "weekly_summary":
        return "\n".join([
            "HL weekly summary",
            "event=weekly_summary",
            f"timestamp={timestamp}",
            f"run_id={_semantic_value(event.get('run_id'))}",
            f"network={_semantic_value(event.get('network'))}",
            f"week_start={_semantic_value(event.get('week_start'))}",
            f"week_end={_semantic_value(event.get('week_end'))}",
            f"current_state={_semantic_value(event.get('current_state'))}",
            f"local_position_qty={_semantic_value(event.get('local_position_qty'))}",
            f"exchange_position_qty={_semantic_value(event.get('exchange_position_qty'))}",
            f"starting_equity={_semantic_value(event.get('starting_equity'))}",
            f"ending_equity={_semantic_value(event.get('ending_equity'))}",
            f"weekly_pnl={_semantic_value(event.get('weekly_pnl'))}",
            f"trade_count={_semantic_value(event.get('trade_count'))}",
            f"reconciliation_status={_semantic_value(event.get('reconciliation_status'))}",
        ])

    if event_type == "semantic":
        return "\n".join([
            "HL semantic",
            f"event={_semantic_value(event.get('event'))}",
            f"timestamp={timestamp}",
            f"run_id={_semantic_value(event.get('run_id'))}",
            f"network={_semantic_value(event.get('network'))}",
            f"state_before={_semantic_value(event.get('state_before'))}",
            f"state_after={_semantic_value(event.get('state_after'))}",
            f"order_id={_semantic_value(event.get('order_id'))}",
            f"qty={_semantic_value(event.get('qty'))}",
            f"fill_qty={_semantic_value(event.get('fill_qty'))}",
            f"price={_semantic_value(event.get('price'))}",
            f"exchange_position_qty={_semantic_value(event.get('exchange_position_qty'))}",
            f"local_position_qty={_semantic_value(event.get('local_position_qty'))}",
        ])

    return "\n".join([
        "HL event",
        f"timestamp={timestamp}",
        f"run_id={_value(event.get('run_id'))}",
        f"network={_value(event.get('network'))}",
        f"type={event_type}",
        f"event={_value(event.get('event'))}",
        f"status={_value(event.get('status'))}",
    ])


def notify_event(notifier: TelegramNotifier, event: Mapping[str, Any]) -> bool:
    return notifier.send_message(format_event(event))
