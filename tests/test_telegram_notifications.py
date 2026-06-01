from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pandas as pd

from scripts import run_live_hl as live_script
from src.core.config_loader import load_config
from src.core.models import (
    ActionType,
    BotState,
    Decision,
    LotRecord,
    OrderResult,
    OrderResultStatus,
    ReconciliationResult,
    ReconciliationStatus,
    ThesisState,
)
from src.hl.account import HLAccountSnapshot, HLPosition
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


def _position_snapshot(qty: float, *, open_orders_count: int = 0) -> HLAccountSnapshot:
    snapshot = _snapshot()
    snapshot.positions = [
        HLPosition(
            coin="BTC",
            qty=qty,
            entry_price=72000.0,
            mark_price=72317.0,
            unrealized_pnl=0.0,
            liquidation_price=None,
            margin_used=0.0,
            leverage=1.0,
        )
    ] if qty else []
    snapshot.perp_positions = snapshot.positions
    snapshot.account_value = abs(qty) * 72317.0
    snapshot.open_orders_count = open_orders_count
    return snapshot


def _filled_order(action: ActionType, qty: float, *, side: str, reduce_only: bool) -> OrderResult:
    return OrderResult(
        status=OrderResultStatus.FILLED,
        action=action,
        side=side,
        reduce_only=reduce_only,
        submitted_qty=qty,
        filled_qty=qty,
        remaining_qty=0.0,
        avg_fill_px=72317.0,
        limit_px=72317.0,
        state_before=BotState.BASE_LONG.value,
        intended_state_after=None,
        applied_state_after=None,
        created_at=datetime.now(timezone.utc),
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


def test_telegram_event_formatting_includes_required_operational_context():
    text = format_event({
        "type": "safety",
        "run_id": "run-1",
        "network": "testnet",
        "event": "bot_startup",
        "coin": "BTC",
        "state": "FLAT",
        "exchange_position_qty": 0.0,
        "local_position_qty": 0.0,
        "dry_run": False,
    })

    assert "run_id=run-1" in text
    assert "network=testnet" in text
    assert "state=FLAT" in text
    assert "exchange_position_qty=0" in text
    assert "local_position_qty=0" in text


def test_duplicate_safety_alerts_are_suppressed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.bot["run_id"] = "run-dup"
    state = ThesisState(symbol="BTC")
    events = []
    live_script._LAST_ALERT_TIMES.clear()
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)
    notifier = telegram.TelegramNotifier(enabled=False)

    for _ in range(2):
        live_script._notify_safety(
            config=cfg,
            notifier=notifier,
            event="exchange_connectivity_lost",
            state=state,
            coin="BTC",
            dry_run=False,
            reason="ConnectionError",
        )

    assert [event["event"] for event in events] == ["exchange_connectivity_lost"]


def test_connectivity_lost_and_restored_alerts_are_emitted(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.bot["run_id"] = "run-connectivity"
    state = ThesisState(symbol="BTC")
    events = []
    snapshots = [RuntimeError("network"), _snapshot()]

    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *a, **kw: pd.DataFrame({"close": [72317.0]}))
    monkeypatch.setattr(live_script, "calc_indicators", lambda *a, **kw: _indicators())
    monkeypatch.setattr(live_script, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(live_script, "engine_run", lambda *a, **kw: Decision(action=ActionType.NO_ACTION, reason="no_trade"))
    monkeypatch.setattr(live_script, "apply_candle_close_funding", lambda state, *a, **kw: state)
    monkeypatch.setattr(live_script, "format_summary", lambda *a, **kw: "summary")
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)

    def fake_snapshot(*args, **kwargs):
        item = snapshots.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(live_script, "get_account_snapshot", fake_snapshot)
    live_script._CONNECTIVITY_LOST = False
    live_script._LAST_ALERT_TIMES.clear()

    for _ in range(2):
        live_script.run_decision_cycle(
            cfg,
            state,
            tmp_path / "state.json",
            MagicMock(),
            TESTNET_WALLET,
            FAKE_KEY,
            dry_run=True,
            notifier=telegram.TelegramNotifier(enabled=False),
        )

    assert any(event.get("event") == "exchange_connectivity_lost" for event in events)
    assert any(event.get("event") == "exchange_connectivity_restored" for event in events)


def test_reconciliation_failed_alert_is_emitted(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.bot["run_id"] = "run-reconcile"
    state = ThesisState(symbol="BTC")
    events = []
    result = ReconciliationResult(
        status=ReconciliationStatus.HALTED,
        reason="mismatch",
        halt_reason="local_exchange_position_mismatch",
        exchange_position_qty=0.01,
        local_position_qty=0.0,
    )

    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *a, **kw: pd.DataFrame({"close": [72317.0]}))
    monkeypatch.setattr(live_script, "calc_indicators", lambda *a, **kw: _indicators())
    monkeypatch.setattr(live_script, "get_account_snapshot", lambda *a, **kw: _position_snapshot(0.01))
    monkeypatch.setattr(live_script, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(live_script, "reconcile_state_with_exchange", lambda *a, **kw: (state, result))
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)
    live_script._LAST_ALERT_TIMES.clear()

    status = live_script.run_decision_cycle(
        cfg,
        state,
        tmp_path / "state.json",
        MagicMock(),
        TESTNET_WALLET,
        FAKE_KEY,
        dry_run=False,
        notifier=telegram.TelegramNotifier(enabled=False),
    )

    assert status == live_script.HALTED
    assert any(event.get("event") == "reconciliation_failed" for event in events)


def test_reconciliation_recovered_alert_is_emitted(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.bot["run_id"] = "run-recovered"
    state = ThesisState(symbol="BTC")
    events = []
    result = ReconciliationResult(
        status=ReconciliationStatus.RECONSTRUCTED_POSITION,
        reason="reconstructed_from_recent_fills",
        exchange_position_qty=0.005,
        local_position_qty=0.0,
        reconstructed_qty=0.005,
    )

    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *a, **kw: pd.DataFrame({"close": [72317.0]}))
    monkeypatch.setattr(live_script, "calc_indicators", lambda *a, **kw: _indicators())
    monkeypatch.setattr(live_script, "get_account_snapshot", lambda *a, **kw: _position_snapshot(0.005))
    monkeypatch.setattr(live_script, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(live_script, "reconcile_state_with_exchange", lambda *a, **kw: (state, result))
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)
    live_script._LAST_ALERT_TIMES.clear()

    status = live_script.run_decision_cycle(
        cfg,
        state,
        tmp_path / "state.json",
        MagicMock(),
        TESTNET_WALLET,
        FAKE_KEY,
        dry_run=False,
        notifier=telegram.TelegramNotifier(enabled=False),
    )

    assert status == live_script.RECONCILED
    assert any(event.get("event") == "reconciliation_recovered" for event in events)


def test_main_one_cycle_emits_startup_and_shutdown_alerts(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.bot["run_id"] = "run-main"
    events = []
    monkeypatch.setattr(live_script, "load_config", lambda path: cfg)
    monkeypatch.setattr(live_script, "startup_checks", lambda config: FAKE_KEY)
    monkeypatch.setattr(live_script, "HyperliquidClient", lambda network: MagicMock())
    monkeypatch.setattr(live_script, "run_decision_cycle", lambda *a, **kw: live_script.CONTINUE)
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)
    live_script._LAST_ALERT_TIMES.clear()
    live_script._STOP_EVENT.clear()

    live_script.main("unused.yaml", one_cycle=True)

    assert any(event.get("event") == "bot_startup" for event in events)
    assert any(event.get("event") == "bot_shutdown" for event in events)
    assert all(event.get("run_id") == "run-main" for event in events if event.get("type") == "safety")


def test_pyramid_added_alert_is_emitted_after_add_fill(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.bot["run_id"] = "run-pyramid"
    state = ThesisState(symbol="BTC", state=BotState.BASE_LONG)
    state.base_lot = LotRecord(lot_id="base", entry_price=72000.0, qty=0.005, entry_date=date(2026, 5, 31))
    state.current_position_qty = 0.005
    state.original_base_qty = 0.005
    state.avg_entry_price = 72000.0
    decision = Decision(action=ActionType.BUY_ADDON, qty=0.002, reason="add_conditions_met", new_state=BotState.PYRAMID_LONG)
    events = []

    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *a, **kw: pd.DataFrame({"close": [72317.0]}))
    monkeypatch.setattr(live_script, "calc_indicators", lambda *a, **kw: _indicators())
    monkeypatch.setattr(live_script, "get_account_snapshot", lambda *a, **kw: _position_snapshot(0.005))
    monkeypatch.setattr(live_script, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(live_script, "engine_run", lambda *a, **kw: decision)
    monkeypatch.setattr(live_script, "_dispatch_broker", lambda *a, **kw: _filled_order(ActionType.BUY_ADDON, 0.002, side="buy", reduce_only=False))
    monkeypatch.setattr(live_script, "apply_candle_close_funding", lambda state, *a, **kw: state)
    monkeypatch.setattr(live_script, "format_summary", lambda *a, **kw: "summary")
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)
    live_script._LAST_ALERT_TIMES.clear()

    live_script.run_decision_cycle(
        cfg,
        state,
        tmp_path / "state.json",
        MagicMock(),
        TESTNET_WALLET,
        FAKE_KEY,
        dry_run=False,
        notifier=telegram.TelegramNotifier(enabled=False),
    )

    assert any(event.get("event") == "pyramid_level_added" for event in events)


def test_runner_activated_and_daily_summary_alerts_are_emitted(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.bot["run_id"] = "run-runner"
    state = ThesisState(symbol="BTC", state=BotState.STARTER_LONG)
    state.base_lot = LotRecord(lot_id="base", entry_price=72000.0, qty=0.005, entry_date=date(2026, 5, 31))
    state.current_position_qty = 0.005
    state.original_base_qty = 0.005
    state.avg_entry_price = 72000.0
    events = []

    def fake_engine(engine_state, *args, **kwargs):
        engine_state.runner_target_qty = 0.0025
        engine_state.runner_mode_active = True
        engine_state.target_price_tp_triggered = True
        return Decision(
            action=ActionType.SELL_TAKE_PROFIT,
            qty=0.0025,
            reason="target_tp_runner_mode",
            new_state=BotState.RUNNER_LONG,
        )

    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *a, **kw: pd.DataFrame({"close": [72317.0]}))
    monkeypatch.setattr(live_script, "calc_indicators", lambda *a, **kw: _indicators())
    monkeypatch.setattr(live_script, "get_account_snapshot", lambda *a, **kw: _position_snapshot(0.005))
    monkeypatch.setattr(live_script, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(live_script, "engine_run", fake_engine)
    monkeypatch.setattr(live_script, "_dispatch_broker", lambda *a, **kw: _filled_order(ActionType.SELL_TAKE_PROFIT, 0.0025, side="sell", reduce_only=True))
    monkeypatch.setattr(live_script, "apply_candle_close_funding", lambda state, *a, **kw: state)
    monkeypatch.setattr(live_script, "format_summary", lambda *a, **kw: "daily state summary")
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)
    live_script._LAST_ALERT_TIMES.clear()

    live_script.run_decision_cycle(
        cfg,
        state,
        tmp_path / "state.json",
        MagicMock(),
        TESTNET_WALLET,
        FAKE_KEY,
        dry_run=False,
        notifier=telegram.TelegramNotifier(enabled=False),
    )

    assert any(event.get("event") == "runner_activated" for event in events)
    assert any(event["type"] == "summary" and "daily state summary" in event["summary"] for event in events)


def test_max_pyramid_reached_alert_is_emitted(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.bot["run_id"] = "run-max-pyramid"
    state = ThesisState(symbol="BTC", state=BotState.PYRAMID_LONG)
    state.current_position_qty = 0.005
    events = []
    decision = Decision(
        action=ActionType.NO_ACTION,
        reason="add_blocked",
        blockers=["max_add_count_reached"],
    )

    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *a, **kw: pd.DataFrame({"close": [72317.0]}))
    monkeypatch.setattr(live_script, "calc_indicators", lambda *a, **kw: _indicators())
    monkeypatch.setattr(live_script, "get_account_snapshot", lambda *a, **kw: _position_snapshot(0.005))
    monkeypatch.setattr(live_script, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(live_script, "engine_run", lambda *a, **kw: decision)
    monkeypatch.setattr(live_script, "apply_candle_close_funding", lambda state, *a, **kw: state)
    monkeypatch.setattr(live_script, "format_summary", lambda *a, **kw: "summary")
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)
    live_script._LAST_ALERT_TIMES.clear()

    live_script.run_decision_cycle(
        cfg,
        state,
        tmp_path / "state.json",
        MagicMock(),
        TESTNET_WALLET,
        FAKE_KEY,
        dry_run=False,
        notifier=telegram.TelegramNotifier(enabled=False),
    )

    assert any(event.get("event") == "max_pyramid_reached" for event in events)
