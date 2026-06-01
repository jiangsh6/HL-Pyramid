from __future__ import annotations

from datetime import date

from scripts import run_live_hl as live_script
from src.core.config_loader import load_config
from src.core.models import BotState, ThesisState
from src.notifications import telegram
from src.notifications.telegram import format_event


def _cfg(tmp_path):
    cfg = load_config("config/mu_long_thesis.yaml")
    cfg.bot["mode"] = "testnet"
    cfg.bot["run_id"] = "weekly-run"
    cfg.logging["run_dir"] = str(tmp_path)
    cfg.notifications.update({
        "enabled": True,
        "telegram_enabled": True,
        "weekly_summary_enabled": True,
    })
    cfg.hl = {
        "network": "testnet",
        "coin": "BTC",
        "wallet_address": "0x1111111111111111111111111111111111111111",
    }
    return cfg


def test_weekly_summary_formatting_includes_required_fields():
    text = format_event({
        "type": "weekly_summary",
        "timestamp": "2026-06-01T00:00:00+00:00",
        "run_id": "run-1",
        "network": "testnet",
        "week_start": "2026-06-01",
        "week_end": "2026-06-07",
        "current_state": "BASE_LONG",
        "local_position_qty": 0.005,
        "exchange_position_qty": 0.005,
        "starting_equity": 1000.0,
        "ending_equity": 1012.5,
        "weekly_pnl": 12.5,
        "trade_count": 3,
        "reconciliation_status": "ok",
    })

    assert "HL weekly summary" in text
    assert "event=weekly_summary" in text
    assert "run_id=run-1" in text
    assert "network=testnet" in text
    assert "week_start=2026-06-01" in text
    assert "week_end=2026-06-07" in text
    assert "current_state=BASE_LONG" in text
    assert "weekly_pnl=12.5" in text
    assert "trade_count=3" in text
    assert "reconciliation_status=ok" in text


def test_weekly_summary_unknown_field_handling():
    text = format_event({"type": "weekly_summary"})

    assert "run_id=unknown" in text
    assert "network=unknown" in text
    assert "week_start=unknown" in text
    assert "ending_equity=unknown" in text
    assert "trade_count=unknown" in text


def test_weekly_summary_sends_once_per_week(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC", state=BotState.FLAT)
    events = []
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)
    live_script._LAST_ALERT_TIMES.clear()

    first = live_script._notify_weekly_summary(
        config=cfg,
        notifier=telegram.TelegramNotifier(enabled=False),
        state=state,
        exchange_position_qty=0.0,
        ending_equity=1000.0,
        reconciliation_status="ok",
        today=date(2026, 6, 1),
    )
    second = live_script._notify_weekly_summary(
        config=cfg,
        notifier=telegram.TelegramNotifier(enabled=False),
        state=state,
        exchange_position_qty=0.0,
        ending_equity=1001.0,
        reconciliation_status="ok",
        today=date(2026, 6, 3),
    )

    assert first is True
    assert second is False
    assert [event["type"] for event in events] == ["weekly_summary"]


def test_weekly_summary_restart_safe_marker_blocks_duplicate(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC", state=BotState.FLAT)
    events = []
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)
    live_script._LAST_ALERT_TIMES.clear()

    assert live_script._notify_weekly_summary(
        config=cfg,
        notifier=telegram.TelegramNotifier(enabled=False),
        state=state,
        today=date(2026, 6, 1),
    ) is True

    live_script._LAST_ALERT_TIMES.clear()
    assert live_script._notify_weekly_summary(
        config=cfg,
        notifier=telegram.TelegramNotifier(enabled=False),
        state=state,
        today=date(2026, 6, 2),
    ) is False
    assert len(events) == 1


def test_weekly_summary_can_send_next_calendar_week(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC", state=BotState.FLAT)
    events = []
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)
    live_script._LAST_ALERT_TIMES.clear()

    assert live_script._notify_weekly_summary(
        config=cfg,
        notifier=telegram.TelegramNotifier(enabled=False),
        state=state,
        today=date(2026, 6, 1),
    ) is True
    assert live_script._notify_weekly_summary(
        config=cfg,
        notifier=telegram.TelegramNotifier(enabled=False),
        state=state,
        today=date(2026, 6, 8),
    ) is True
    assert [event["week_start"] for event in events] == ["2026-06-01", "2026-06-08"]
