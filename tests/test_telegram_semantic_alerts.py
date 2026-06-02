from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pandas as pd

from scripts import cancel_hl_order as cancel_script
from scripts import run_live_hl as live_script
from src.core.config_loader import load_config
from src.core.models import (
    ActionType,
    BotState,
    Decision,
    LotRecord,
    OrderResult,
    OrderResultStatus,
    ThesisState,
)
from src.hl.account import HLAccountSnapshot, HLPosition
from src.notifications import telegram
from src.notifications.telegram import format_event

TESTNET_WALLET = "0x1111111111111111111111111111111111111111"
FAKE_KEY = "0x" + "a" * 64


def _cfg(tmp_path):
    cfg = load_config("config/mu_long_thesis.yaml")
    cfg.bot["mode"] = "testnet"
    cfg.bot["run_id"] = "semantic-run"
    cfg.data["source"] = "hyperliquid"
    cfg.logging["run_dir"] = str(tmp_path)
    cfg.notifications.update({
        "enabled": True,
        "telegram_enabled": True,
        "heartbeat_enabled": True,
        "trade_alerts_enabled": True,
        "decision_alerts_enabled": True,
    })
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


def _snapshot(qty: float = 0.0) -> HLAccountSnapshot:
    positions = [
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
    return HLAccountSnapshot(
        account_value=abs(qty) * 72317.0,
        margin_used=0.0,
        withdrawable=0.0,
        positions=positions,
        perp_account_value=abs(qty) * 72317.0,
        perp_margin_used=0.0,
        perp_withdrawable=0.0,
        perp_positions=positions,
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


def _result(action: ActionType, status: OrderResultStatus, *, filled: float, submitted: float | None = None) -> OrderResult:
    submitted_qty = submitted if submitted is not None else filled
    return OrderResult(
        status=status,
        oid=12345,
        action=action,
        side="buy" if action in {ActionType.BUY_STARTER, ActionType.BUY_BASE, ActionType.BUY_ADDON} else "sell",
        reduce_only=action not in {ActionType.BUY_STARTER, ActionType.BUY_BASE, ActionType.BUY_ADDON},
        submitted_qty=submitted_qty,
        filled_qty=filled,
        remaining_qty=max(submitted_qty - filled, 0.0),
        avg_fill_px=72317.0 if filled else None,
        limit_px=72317.0,
        state_before=None,
        intended_state_after=None,
        applied_state_after=None,
        created_at=datetime.now(timezone.utc),
    )


def _long_state(qty: float = 0.005, state: BotState = BotState.BASE_LONG) -> ThesisState:
    thesis = ThesisState(symbol="BTC", state=state)
    thesis.base_lot = LotRecord(lot_id="base", entry_price=72000.0, qty=qty, entry_date=date(2026, 5, 31))
    thesis.current_position_qty = qty
    thesis.original_base_qty = qty
    thesis.avg_entry_price = 72000.0
    return thesis


def _patch_cycle(monkeypatch, *, snapshot, decision, order_result, events):
    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *a, **kw: pd.DataFrame({"close": [72317.0]}))
    monkeypatch.setattr(live_script, "calc_indicators", lambda *a, **kw: _indicators())
    monkeypatch.setattr(live_script, "get_account_snapshot", lambda *a, **kw: snapshot)
    monkeypatch.setattr(live_script, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(live_script, "engine_run", lambda *a, **kw: decision)
    monkeypatch.setattr(live_script, "_dispatch_broker", lambda *a, **kw: order_result)
    monkeypatch.setattr(live_script, "apply_candle_close_funding", lambda state, *a, **kw: state)
    monkeypatch.setattr(live_script, "format_summary", lambda *a, **kw: "summary")
    monkeypatch.setattr(live_script, "notify_event", lambda notifier, event: events.append(event) or True)
    live_script._LAST_ALERT_TIMES.clear()
    live_script._STOP_EVENT.clear()


def _run_cycle(tmp_path, monkeypatch, state, decision, order_result, snapshot_qty):
    cfg = _cfg(tmp_path)
    events = []
    _patch_cycle(
        monkeypatch,
        snapshot=_snapshot(snapshot_qty),
        decision=decision,
        order_result=order_result,
        events=events,
    )
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
    return events


def _semantic_events(events):
    return [event["event"] for event in events if event.get("type") == "semantic"]


def test_semantic_formatting_uses_unknown_for_missing_fields():
    text = format_event({"type": "semantic", "event": "position_opened"})

    assert "✅ HL Trading Alert" in text
    assert "Event: position_opened" in text
    assert "Run:\n-" in text
    assert "State Before: -" in text
    assert "Order ID: -" in text


def test_semantic_notifier_failures_are_fail_soft(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(live_script, "notify_event", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("telegram down")))

    live_script._notify_semantic(
        config=cfg,
        notifier=telegram.TelegramNotifier(enabled=True, token="token", chat_id="123"),
        event="position_opened",
        state_before="FLAT",
        state_after="STARTER_LONG",
    )


def test_position_opened_and_full_fill_alerts(tmp_path, monkeypatch):
    events = _run_cycle(
        tmp_path,
        monkeypatch,
        ThesisState(symbol="BTC"),
        Decision(action=ActionType.BUY_STARTER, qty=0.005, reason="starter", new_state=BotState.STARTER_LONG),
        _result(ActionType.BUY_STARTER, OrderResultStatus.FILLED, filled=0.005),
        0.0,
    )

    semantic = _semantic_events(events)
    assert "position_opened" in semantic
    assert "full_fill_detected" in semantic


def test_position_closed_stop_triggered_stop_filled_and_full_fill_alerts(tmp_path, monkeypatch):
    events = _run_cycle(
        tmp_path,
        monkeypatch,
        _long_state(),
        Decision(action=ActionType.SELL_STOP, qty=0.005, reason="hard_stop", new_state=BotState.EXITED),
        _result(ActionType.SELL_STOP, OrderResultStatus.FILLED, filled=0.005),
        0.005,
    )

    semantic = _semantic_events(events)
    assert "stop_loss_triggered" in semantic
    assert "stop_loss_filled" in semantic
    assert "position_closed" in semantic
    assert "full_fill_detected" in semantic


def test_take_profit_triggered_and_filled_alerts(tmp_path, monkeypatch):
    events = _run_cycle(
        tmp_path,
        monkeypatch,
        _long_state(),
        Decision(action=ActionType.SELL_TAKE_PROFIT, qty=0.0025, reason="target_tp", new_state=BotState.RUNNER_LONG),
        _result(ActionType.SELL_TAKE_PROFIT, OrderResultStatus.FILLED, filled=0.0025),
        0.005,
    )

    semantic = _semantic_events(events)
    assert "take_profit_triggered" in semantic
    assert "take_profit_filled" in semantic
    assert "full_fill_detected" in semantic


def test_partial_fill_detected_alert(tmp_path, monkeypatch):
    events = _run_cycle(
        tmp_path,
        monkeypatch,
        _long_state(),
        Decision(action=ActionType.SELL_TAKE_PROFIT, qty=0.0025, reason="tp_partial"),
        _result(ActionType.SELL_TAKE_PROFIT, OrderResultStatus.PARTIALLY_FILLED, filled=0.001, submitted=0.0025),
        0.005,
    )

    assert "partial_fill_detected" in _semantic_events(events)


def test_emergency_exit_filled_alert(tmp_path, monkeypatch):
    events = _run_cycle(
        tmp_path,
        monkeypatch,
        _long_state(),
        Decision(action=ActionType.EXIT_ALL, qty=0.005, reason="manual_flatten", new_state=BotState.EXITED),
        _result(ActionType.EXIT_ALL, OrderResultStatus.FILLED, filled=0.005),
        0.005,
    )

    assert "emergency_exit_filled" in _semantic_events(events)


def test_order_cancelled_alert(monkeypatch):
    events = []
    cfg = load_config("config/mu_long_thesis.yaml")
    cfg.bot["mode"] = "testnet"
    cfg.bot["run_id"] = "cancel-run"
    cfg.notifications.update({
        "enabled": True,
        "telegram_enabled": True,
        "trade_alerts_enabled": True,
    })
    state = _long_state()
    order_result = _result(ActionType.EXIT_ALL, OrderResultStatus.CANCELED, filled=0.0, submitted=0.005)
    order_result.oid = 999
    order_result.state_before = "HALTED"
    order_result.applied_state_after = "BASE_LONG"
    monkeypatch.setattr(cancel_script, "notify_event", lambda notifier, event: events.append(event) or True)

    cancel_script._notify_order_cancelled(cfg, state, order_result, 0.005)

    assert _semantic_events(events) == ["order_cancelled"]
