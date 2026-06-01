from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pandas as pd

from scripts import run_live_hl as live_script
from src.core.config_loader import load_config
from src.core.models import ActionType, BotState, Decision, ThesisState
from src.hl.account import HLAccountSnapshot
from src.notifications import telegram
from src.notifications.telegram import alert_toggle_enabled, format_event


TESTNET_WALLET = "0x1111111111111111111111111111111111111111"
FAKE_KEY = "0x" + "a" * 64


def _notify_cfg():
    return {
        "enabled": True,
        "telegram_enabled": True,
        "heartbeat_enabled": True,
        "trade_alerts_enabled": True,
        "decision_alerts_enabled": True,
    }


def test_missing_env_vars_disable_telegram_safely(monkeypatch, caplog):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    caplog.set_level("INFO", logger="telegram-test")
    notifier = telegram.TelegramNotifier.from_config(
        _notify_cfg(),
        logger=telegram.logging.getLogger("telegram-test"),
    )
    assert notifier.enabled is False
    assert "alert_disabled=true" in caplog.text


def test_send_message_does_not_expose_token(monkeypatch, caplog):
    token = "SECRET_TOKEN_123"
    notifier = telegram.TelegramNotifier(
        enabled=True,
        token=token,
        chat_id="123",
        logger=telegram.logging.getLogger("telegram-secret-test"),
    )
    monkeypatch.setattr(
        telegram.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network failed")),
    )
    caplog.set_level("WARNING", logger="telegram-secret-test")

    assert notifier.send_message("hello") is False
    assert token not in caplog.text


def test_telegram_failure_does_not_raise(monkeypatch):
    notifier = telegram.TelegramNotifier(enabled=True, token="token", chat_id="123")
    monkeypatch.setattr(
        telegram.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert notifier.send_message("hello") is False


def test_heartbeat_alert_formatting_includes_key_fields():
    text = format_event({
        "type": "heartbeat",
        "timestamp": "2026-06-01T00:00:00+00:00",
        "network": "testnet",
        "coin": "BTC",
        "state": "FLAT",
        "exchange_position_qty": 0.0,
        "local_position_qty": 0.0,
        "open_orders_count": 0,
        "pending_order_status": None,
        "pending_order_oid": None,
        "halted": False,
        "halt_reason": None,
        "last_decision": "CONTINUE",
        "dry_run": True,
    })
    assert "HL heartbeat" in text
    assert "network=testnet" in text
    assert "coin=BTC" in text
    assert "open_orders_count=0" in text
    assert "dry_run=True" in text


def test_decision_alert_formatting_includes_decision_and_dry_run():
    text = format_event({
        "type": "decision",
        "coin": "BTC",
        "decision": "buy_starter",
        "reason": "starter_entry_conditions_met",
        "state_before": "FLAT",
        "price": 72335.0,
        "dry_run": True,
    })
    assert "HL decision" in text
    assert "decision=buy_starter" in text
    assert "dry_run=True" in text


def test_order_alert_formatting_includes_order_fields():
    text = format_event({
        "type": "order",
        "status": "submitted_unfilled",
        "action": "sell_stop",
        "reduce_only": True,
        "side": "sell",
        "qty": 0.005,
        "px": 70000.0,
        "oid": 123,
        "filled_qty": 0.0,
        "remaining_qty": 0.005,
    })
    assert "HL order" in text
    assert "side=sell" in text
    assert "qty=0.005" in text
    assert "px=70000" in text
    assert "oid=123" in text
    assert "status=submitted_unfilled" in text


def test_safety_alert_formatting_includes_halt_reason():
    text = format_event({
        "type": "safety",
        "event": "HALTED",
        "coin": "BTC",
        "state": "HALTED",
        "halted": True,
        "halt_reason": "pending_order_missing_on_exchange",
        "dry_run": False,
    })
    assert "HL safety" in text
    assert "halt_reason=pending_order_missing_on_exchange" in text


def test_config_toggle_disables_alerts():
    assert alert_toggle_enabled({"enabled": False, "telegram_enabled": True, "heartbeat_enabled": True}, "heartbeat_enabled") is False
    assert alert_toggle_enabled({"enabled": True, "telegram_enabled": False, "heartbeat_enabled": True}, "heartbeat_enabled") is False
    assert alert_toggle_enabled({"enabled": True, "telegram_enabled": True, "heartbeat_enabled": False}, "heartbeat_enabled") is False


def _cfg(tmp_path):
    cfg = load_config("config/mu_long_thesis.yaml")
    cfg.bot["mode"] = "testnet"
    cfg.data["source"] = "hyperliquid"
    cfg.logging["run_dir"] = str(tmp_path)
    cfg.notifications.update(_notify_cfg())
    cfg.hl = {
        "network": "testnet",
        "coin": "BTC",
        "wallet_address": TESTNET_WALLET,
        "bar_interval": "4h",
        "sz_decimals": 5,
        "min_size": 0.001,
        "collateral_mode": "unified",
    }
    return cfg


def _indicators():
    class _Ind:
        date = date(2026, 5, 31)
        adj_close = 72317.0
        close = 72317.0
        ma5 = ma10 = ma20 = ma50 = atr14 = 50000.0
        prior_highest_high_20d = 50000.0
        avg_volume_20d = 1000.0
        open = high = low = 50000.0
        volume = 1000.0
        prev_adj_close = 50000.0
        intraday_return = 0.0
        gap_up_pct = gap_down_pct = 0.0
        distance_from_ma10 = distance_from_ma20 = 0.0
        drawdown_from_20d_high = 0.0
        bar_time = None
    return _Ind()


def _snapshot() -> HLAccountSnapshot:
    return HLAccountSnapshot(
        account_value=0.0,
        margin_used=0.0,
        withdrawable=0.0,
        positions=[],
        perp_account_value=0.0,
        perp_margin_used=0.0,
        perp_withdrawable=0.0,
        perp_positions=[],
        spot_usdc_total=972.84,
        spot_usdc_hold=0.0,
        spot_usdc_available=972.84,
        open_orders_count=0,
        open_orders=[],
        collateral_mode="unified",
        effective_trading_collateral=972.84,
        trading_collateral_available=True,
        collateral_reason="unified_spot_usdc_available",
        perp_collateral_available=False,
    )


def test_dry_run_order_blocked_event_sends_alert_when_enabled(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = ThesisState(symbol="BTC")
    decision = Decision(
        action=ActionType.BUY_STARTER,
        qty=0.001,
        reason="starter_entry_conditions_met",
        new_state=BotState.STARTER_LONG,
    )
    events = []
    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *a, **kw: pd.DataFrame({"close": [72317.0]}))
    monkeypatch.setattr(live_script, "calc_indicators", lambda *a, **kw: _indicators())
    monkeypatch.setattr(live_script, "get_account_snapshot", lambda *a, **kw: _snapshot())
    monkeypatch.setattr(live_script, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(live_script, "engine_run", lambda *a, **kw: decision)
    monkeypatch.setattr(live_script, "apply_candle_close_funding", lambda state, *a, **kw: state)
    monkeypatch.setattr(live_script, "format_summary", lambda *a, **kw: "")
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)

    status = live_script.run_decision_cycle(
        cfg,
        state,
        tmp_path / "state.json",
        MagicMock(),
        TESTNET_WALLET,
        FAKE_KEY,
        dry_run=True,
        notifier=telegram.TelegramNotifier(enabled=False),
    )

    assert status == live_script.CONTINUE
    assert any(event["type"] == "decision" for event in events)
    assert any(event["type"] == "safety" and event["event"] == "dry_run_order_blocked" for event in events)
