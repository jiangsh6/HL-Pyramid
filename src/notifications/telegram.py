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
    if isinstance(value, str) and value.strip().lower() == "unknown":
        return "-"
    if isinstance(value, float):
        return f"{value:.8g}"
    return str(value)


def _semantic_value(value: Any) -> str:
    return _value(value)


def _title_value(value: Any) -> str:
    rendered = _value(value)
    return rendered.title() if rendered != "-" else rendered


def _mode_label(value: Any) -> str:
    if isinstance(value, bool):
        return "Dry Run" if value else "Live"
    return _title_value(value)


def _label_line(label: str, value: Any) -> str:
    return f"{label}: {_value(value)}"


def _common_context(event: Mapping[str, Any], *, state_key: str = "state") -> list[str]:
    return [
        _label_line("Coin", event.get("coin")),
        _label_line("Network", _title_value(event.get("network"))),
        _label_line("State", event.get(state_key)),
    ]


def _run_block(event: Mapping[str, Any]) -> list[str]:
    return ["", "Run:", _value(event.get("run_id"))]


def _qty_block(event: Mapping[str, Any]) -> list[str]:
    return [
        "",
        _label_line("Exchange Qty", event.get("exchange_position_qty")),
        _label_line("Local Qty", event.get("local_position_qty")),
    ]


def _safety_title(event_name: str, halted: Any) -> str:
    normalized = event_name.lower()
    if normalized == "bot_startup":
        return "🚀 HL Bot Started"
    if normalized == "bot_shutdown":
        return "🛑 HL Bot Stopped"
    if normalized == "telegram_health_check":
        return "✅ Telegram Health Check"
    if "halt" in normalized or halted is True:
        return "🚨 HL Bot Halted"
    if "connectivity_lost" in normalized:
        return "📡 HL Connectivity Lost"
    if "connectivity_restored" in normalized:
        return "📡 HL Connectivity Restored"
    if "reconciliation_failed" in normalized:
        return "🚨 HL Reconciliation Failed"
    if "reconciliation_recovered" in normalized:
        return "✅ HL Reconciliation Recovered"
    return "⚠️ HL Safety Alert"


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
            "💓 HL Heartbeat",
            "",
            *_common_context(event),
            _label_line("Mode", _mode_label(event.get("dry_run"))),
            *_qty_block(event),
            _label_line("Open Orders", event.get("open_orders_count")),
            _label_line("Pending", f"{_value(event.get('pending_order_status'))}/{_value(event.get('pending_order_oid'))}"),
            _label_line("Halted", event.get("halted")),
            _label_line("Reason", event.get("halt_reason")),
            _label_line("Last Decision", event.get("last_decision")),
            "",
            _label_line("Timestamp", timestamp),
            *_run_block(event),
        ])

    if event_type == "decision":
        return "\n".join([
            "🧭 HL Decision",
            "",
            *_common_context(event),
            _label_line("Mode", _mode_label(event.get("dry_run"))),
            _label_line("Decision", event.get("decision")),
            _label_line("Reason", event.get("reason")),
            _label_line("State Before", event.get("state_before")),
            *_qty_block(event),
            _label_line("Price", event.get("price")),
            _label_line("Mark", event.get("mark")),
            _label_line("Oracle", event.get("oracle")),
            "",
            _label_line("Timestamp", timestamp),
            *_run_block(event),
        ])

    if event_type == "order":
        return "\n".join([
            "📨 HL Order Update",
            "",
            *_common_context(event),
            _label_line("Status", event.get("status")),
            _label_line("Action", event.get("action")),
            _label_line("Side", event.get("side")),
            _label_line("Reduce Only", event.get("reduce_only")),
            _label_line("Qty", event.get("qty")),
            _label_line("Filled", event.get("filled_qty")),
            _label_line("Remaining", event.get("remaining_qty")),
            _label_line("Price", event.get("px")),
            _label_line("OID", event.get("oid")),
            *_qty_block(event),
            "",
            _label_line("Timestamp", timestamp),
            *_run_block(event),
        ])

    if event_type == "safety":
        event_name = _value(event.get("event"))
        return "\n".join([
            _safety_title(event_name, event.get("halted")),
            "",
            *_common_context(event),
            _label_line("Mode", _mode_label(event.get("dry_run"))),
            _label_line("Event", event.get("event")),
            "",
            "Reason:",
            _value(event.get("halt_reason") or event.get("reason") or event.get("event")),
            *_qty_block(event),
            _label_line("Halted", event.get("halted")),
            _label_line("Config", event.get("config_path")),
            "",
            _label_line("Timestamp", timestamp),
            *_run_block(event),
        ])

    if event_type == "summary":
        return "\n".join([
            "📊 HL Daily Summary",
            "",
            *_common_context(event),
            *_qty_block(event),
            "",
            "Summary:",
            _value(event.get("summary")),
            "",
            _label_line("Timestamp", timestamp),
            *_run_block(event),
        ])

    if event_type == "signal_check":
        passing = event.get("passing") or []
        blocking = event.get("blocking") or []
        passing_lines = [f"✅ {item}" for item in passing[:5]] or ["-"]
        blocking_lines = [f"❌ {item}" for item in blocking[:5]] or ["-"]
        return "\n".join([
            "🧭 HYPE Signal Check" if _value(event.get("coin")) == "HYPE" else "🧭 HL Signal Check",
            "",
            _label_line("State", event.get("state")),
            "",
            "Action:",
            _value(event.get("action")),
            "",
            "Readiness:",
            f"{_value(event.get('readiness_score'))}/100",
            "",
            "Closest Trigger:",
            _value(event.get("closest_trigger")),
            f"Needs {_value(event.get('closest_trigger_distance'))}",
            "",
            "Passing:",
            *passing_lines,
            "",
            "Blocking:",
            *blocking_lines,
            "",
            "Risk:",
            f"${_value(event.get('risk_budget_remaining'))} remaining of ${_value(event.get('max_risk'))}",
            "",
            "Next Check:",
            "1h" if _value(event.get("coin")) == "HYPE" else "-",
            "",
            _label_line("Timestamp", timestamp),
            *_run_block(event),
        ])

    if event_type == "strategy_audit":
        return "\n".join([
            "🧪 HYPE Strategy Audit" if _value(event.get("coin")) == "HYPE" else "🧪 HL Strategy Audit",
            "",
            _label_line("Parameters Reviewed", event.get("parameters_reviewed")),
            _label_line("Warnings", event.get("warning_count")),
            "",
            _label_line("Most Permissive", event.get("most_permissive")),
            _label_line("Most Restrictive", event.get("most_restrictive")),
            "",
            _label_line("Current Signal", event.get("current_signal")),
            _label_line("Readiness", event.get("readiness_score")),
            "",
            _label_line("Audit Report", event.get("report_path")),
            "",
            _label_line("Timestamp", timestamp),
            *_run_block(event),
        ])

    if event_type == "weekly_summary":
        return "\n".join([
            "📅 HL Weekly Summary",
            "",
            _label_line("Network", _title_value(event.get("network"))),
            _label_line("Week Start", event.get("week_start")),
            _label_line("Week End", event.get("week_end")),
            _label_line("State", event.get("current_state")),
            *_qty_block(event),
            _label_line("Starting Equity", event.get("starting_equity")),
            _label_line("Ending Equity", event.get("ending_equity")),
            _label_line("Weekly PnL", event.get("weekly_pnl")),
            _label_line("Trades", event.get("trade_count")),
            _label_line("Reconciliation", event.get("reconciliation_status")),
            "",
            _label_line("Timestamp", timestamp),
            *_run_block(event),
        ])

    if event_type == "semantic":
        name = _semantic_value(event.get("event"))
        return "\n".join([
            "✅ HL Trading Alert" if "filled" in name or "opened" in name or "closed" in name else "⚠️ HL Trading Alert",
            "",
            _label_line("Event", name),
            _label_line("Network", _title_value(event.get("network"))),
            _label_line("State Before", event.get("state_before")),
            _label_line("State After", event.get("state_after")),
            _label_line("Action", event.get("action")),
            _label_line("Order ID", event.get("order_id")),
            _label_line("Qty", event.get("qty")),
            _label_line("Fill Qty", event.get("fill_qty")),
            _label_line("Price", event.get("price")),
            *_qty_block(event),
            "",
            _label_line("Timestamp", timestamp),
            *_run_block(event),
        ])

    return "\n".join([
        "ℹ️ HL Event",
        "",
        _label_line("Type", event_type),
        _label_line("Event", event.get("event")),
        _label_line("Status", event.get("status")),
        _label_line("Network", _title_value(event.get("network"))),
        "",
        _label_line("Timestamp", timestamp),
        *_run_block(event),
    ])


def notify_event(notifier: TelegramNotifier, event: Mapping[str, Any]) -> bool:
    return notifier.send_message(format_event(event))
