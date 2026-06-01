from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from scripts import cancel_hl_order, clear_false_hl_halt, flatten_hl_position
from src.core.config_loader import load_config
from src.core.models import (
    ActionType,
    BotState,
    LotRecord,
    OrderResult,
    OrderResultStatus,
    PendingOrder,
    ReconciliationResult,
    ReconciliationStatus,
    ThesisState,
)
from src.hl.account import HLAccountSnapshot, HLOpenOrder, HLPosition
from src.reporting.state_writer import write_state


def _mainnet_cfg(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0x1111111111111111111111111111111111111111")
    cfg = load_config("config/btc_testnet_realistic_probe.yaml")
    cfg.bot["mode"] = "mainnet"
    cfg.bot["mainnet_confirmed"] = True
    cfg.logging["run_dir"] = str(tmp_path)
    cfg.hl = dict(cfg.hl or {})
    cfg.hl.update(
        {
            "network": "mainnet",
            "coin": "BTC",
            "wallet_address": "0x1111111111111111111111111111111111111111",
            "sz_decimals": 5,
            "min_size": 0.001,
        }
    )
    cfg.notifications["enabled"] = False
    cfg.notifications["telegram_enabled"] = False
    return cfg


def _snapshot(*, qty: float = 0.005, open_orders: list[HLOpenOrder] | None = None) -> HLAccountSnapshot:
    positions = []
    if qty:
        positions = [
            HLPosition(
                coin="BTC",
                qty=qty,
                entry_price=72000.0,
                mark_price=72000.0,
                unrealized_pnl=0.0,
                liquidation_price=None,
                margin_used=10.0,
                leverage=1.0,
            )
        ]
    orders = open_orders or []
    return HLAccountSnapshot(
        account_value=100.0,
        margin_used=10.0,
        withdrawable=90.0,
        positions=positions,
        perp_account_value=100.0,
        perp_withdrawable=90.0,
        perp_positions=positions,
        open_orders_count=len(orders),
        open_orders=orders,
        effective_trading_collateral=100.0,
        trading_collateral_available=True,
    )


def _long_state() -> ThesisState:
    state = ThesisState(symbol="BTC", state=BotState.BASE_LONG)
    state.current_position_qty = 0.005
    state.original_base_qty = 0.005
    state.avg_entry_price = 72000.0
    state.sz_decimals = 5
    state.base_lot = LotRecord(
        lot_id="base",
        entry_price=72000.0,
        qty=0.005,
        entry_date=date(2026, 6, 1),
    )
    return state


def _pending_exit() -> PendingOrder:
    return PendingOrder(
        oid=12345,
        symbol="BTC",
        action="exit_all",
        side="sell",
        reduce_only=True,
        qty=0.005,
        qty_submitted=0.005,
        qty_remaining=0.005,
        limit_px=71900.0,
        status="submitted_unfilled",
        created_at=datetime.now(timezone.utc),
    )


def test_clear_false_local_mismatch_halt_after_reduce_recovery_aligns():
    state = _long_state()
    state.state = BotState.HALTED
    state.halted = True
    state.halt_reason = "local_exchange_position_mismatch"
    state.current_position_qty = 0.00656
    state.addon_lots = [
        LotRecord(
            lot_id="add_1",
            entry_price=72100.0,
            qty=0.00156,
            entry_date=date(2026, 6, 1),
        )
    ]
    state.pending_order = None

    cleared = cancel_hl_order.clear_false_local_mismatch_halt(
        state,
        exchange_qty=0.00656,
        open_orders_count=0,
    )

    assert cleared is True
    assert state.state == BotState.PYRAMID_LONG
    assert state.halted is False
    assert state.halt_reason is None


def test_clear_false_local_mismatch_halt_refuses_mismatched_qty():
    state = _long_state()
    state.state = BotState.HALTED
    state.halted = True
    state.halt_reason = "local_exchange_position_mismatch"

    cleared = cancel_hl_order.clear_false_local_mismatch_halt(
        state,
        exchange_qty=0.004,
        open_orders_count=0,
    )

    assert cleared is False
    assert state.state == BotState.HALTED
    assert state.halted is True
    assert state.halt_reason == "local_exchange_position_mismatch"


def test_clear_false_local_mismatch_halt_refuses_pending_order():
    state = _long_state()
    state.state = BotState.HALTED
    state.halted = True
    state.halt_reason = "local_exchange_position_mismatch"
    state.pending_order = _pending_exit()

    cleared = cancel_hl_order.clear_false_local_mismatch_halt(
        state,
        exchange_qty=state.current_position_qty,
        open_orders_count=0,
    )

    assert cleared is False
    assert state.state == BotState.HALTED
    assert state.pending_order is not None


def test_clear_false_halt_script_clears_when_exchange_and_local_qty_match(tmp_path, monkeypatch, capsys):
    cfg = _mainnet_cfg(tmp_path, monkeypatch)
    cfg.bot["mode"] = "testnet"
    cfg.hl["network"] = "testnet"
    cfg.notifications["enabled"] = False
    state = _long_state()
    state.state = BotState.HALTED
    state.halted = True
    state.halt_reason = "local_exchange_position_mismatch"
    write_state(state, str(tmp_path / "state.json"))
    monkeypatch.setattr(clear_false_hl_halt, "load_config", lambda _: cfg)
    monkeypatch.setattr(clear_false_hl_halt, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(clear_false_hl_halt, "get_account_snapshot", lambda *a, **kw: _snapshot(qty=0.005))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "clear_false_hl_halt.py",
            "--config",
            "ignored.yaml",
            "--coin",
            "BTC",
            "--confirm",
            "--one-cycle",
        ],
    )

    rc = clear_false_hl_halt.main()

    assert rc == 0
    assert "CLEARED_FALSE_HALT" in capsys.readouterr().out


def test_mainnet_recovery_guard_blocks_without_mainnet_flag(tmp_path, monkeypatch):
    cfg = _mainnet_cfg(tmp_path, monkeypatch)
    monkeypatch.setenv("HL_ALLOW_MAINNET", "true")

    with pytest.raises(ValueError, match="--mainnet"):
        cancel_hl_order._validate_recovery_network_intent(cfg, "mainnet", False)

    with pytest.raises(ValueError, match="--mainnet"):
        flatten_hl_position._validate_recovery_network_intent(cfg, "mainnet", False)


def test_mainnet_recovery_guard_blocks_without_env(tmp_path, monkeypatch):
    cfg = _mainnet_cfg(tmp_path, monkeypatch)
    monkeypatch.delenv("HL_ALLOW_MAINNET", raising=False)

    with pytest.raises(ValueError, match="HL_ALLOW_MAINNET"):
        cancel_hl_order._validate_recovery_network_intent(cfg, "mainnet", True)


def test_mainnet_recovery_guard_allows_only_all_gates(tmp_path, monkeypatch):
    cfg = _mainnet_cfg(tmp_path, monkeypatch)
    monkeypatch.setenv("HL_ALLOW_MAINNET", "true")

    assert cancel_hl_order._validate_recovery_network_intent(cfg, "mainnet", True) is True
    assert flatten_hl_position._validate_recovery_network_intent(cfg, "mainnet", True) is True


def test_cancel_mainnet_requires_exact_oid(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cancel_hl_order.py",
            "--config",
            "ignored.yaml",
            "--coin",
            "BTC",
            "--mainnet",
            "--confirm",
            "--one-cycle",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        cancel_hl_order.main()

    assert exc.value.code == 2


def test_recovery_scripts_require_confirm_and_one_cycle(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "flatten_hl_position.py",
            "--config",
            "ignored.yaml",
            "--coin",
            "BTC",
            "--mainnet",
        ],
    )

    assert flatten_hl_position.main() == 2


def test_flatten_mainnet_refuses_open_orders_before_order(tmp_path, monkeypatch, capsys):
    cfg = _mainnet_cfg(tmp_path, monkeypatch)
    state_path = tmp_path / "state.json"
    write_state(_long_state(), str(state_path))
    submitted = {"called": False}
    monkeypatch.setenv("HL_ALLOW_MAINNET", "true")
    monkeypatch.setattr(flatten_hl_position, "load_config", lambda *_: cfg)
    monkeypatch.setattr(flatten_hl_position, "get_private_key", lambda *_: "secret")
    monkeypatch.setattr(flatten_hl_position, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(
        flatten_hl_position,
        "get_account_snapshot",
        lambda *a, **kw: _snapshot(
            open_orders=[HLOpenOrder(oid=999, coin="BTC", side="sell", limit_px=71900.0, status="open", qty=0.005)]
        ),
    )
    monkeypatch.setattr(flatten_hl_position, "execute_hl", lambda *a, **kw: submitted.__setitem__("called", True))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "flatten_hl_position.py",
            "--config",
            "ignored.yaml",
            "--coin",
            "BTC",
            "--mainnet",
            "--confirm",
            "--one-cycle",
        ],
    )

    rc = flatten_hl_position.main()

    assert rc == 1
    assert submitted["called"] is False
    assert "STOPPED_BEFORE_EXIT: open_orders_count=1" in capsys.readouterr().out


def test_flatten_mainnet_submits_reduce_only_only(tmp_path, monkeypatch):
    cfg = _mainnet_cfg(tmp_path, monkeypatch)
    state = _long_state()
    write_state(state, str(tmp_path / "state.json"))
    submitted = {"reduce_only": None}
    monkeypatch.setenv("HL_ALLOW_MAINNET", "true")
    monkeypatch.setattr(flatten_hl_position, "load_config", lambda *_: cfg)
    monkeypatch.setattr(flatten_hl_position, "get_private_key", lambda *_: "secret")
    monkeypatch.setattr(flatten_hl_position, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(flatten_hl_position, "get_account_snapshot", lambda *a, **kw: _snapshot())
    monkeypatch.setattr(flatten_hl_position, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(
        flatten_hl_position,
        "reconcile_state_with_exchange",
        lambda local_state, *a, **kw: (
            local_state,
            ReconciliationResult(
                status=ReconciliationStatus.OK,
                reason="ok",
                exchange_position_qty=0.005,
                local_position_qty=0.005,
            ),
        ),
    )

    def fake_execute(decision, *_args, **_kwargs):
        assert decision.action == ActionType.EXIT_ALL
        submitted["reduce_only"] = True
        return OrderResult(
            status=OrderResultStatus.SUBMITTED_UNFILLED,
            action=ActionType.EXIT_ALL,
            side="sell",
            reduce_only=True,
            submitted_qty=0.005,
            filled_qty=0.0,
            remaining_qty=0.005,
            limit_px=71900.0,
            raw_status_sanitized="submitted_unfilled",
            state_before=BotState.BASE_LONG.value,
            intended_state_after=BotState.EXITED.value,
            applied_state_after=BotState.BASE_LONG.value,
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(flatten_hl_position, "execute_hl", fake_execute)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "flatten_hl_position.py",
            "--config",
            "ignored.yaml",
            "--coin",
            "BTC",
            "--mainnet",
            "--confirm",
            "--one-cycle",
        ],
    )

    rc = flatten_hl_position.main()

    assert rc == 1
    assert submitted["reduce_only"] is True


def test_flatten_mainnet_does_not_print_private_key(tmp_path, monkeypatch, capsys):
    cfg = _mainnet_cfg(tmp_path, monkeypatch)
    write_state(_long_state(), str(tmp_path / "state.json"))
    monkeypatch.setenv("HL_ALLOW_MAINNET", "true")
    monkeypatch.setattr(flatten_hl_position, "load_config", lambda *_: cfg)
    monkeypatch.setattr(flatten_hl_position, "get_private_key", lambda *_: "super_secret_key")
    monkeypatch.setattr(flatten_hl_position, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(
        flatten_hl_position,
        "get_account_snapshot",
        lambda *a, **kw: _snapshot(open_orders=[HLOpenOrder(oid=999, coin="BTC", side="sell", limit_px=71900.0, status="open", qty=0.005)]),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "flatten_hl_position.py",
            "--config",
            "ignored.yaml",
            "--coin",
            "BTC",
            "--mainnet",
            "--confirm",
            "--one-cycle",
        ],
    )

    flatten_hl_position.main()

    assert "super_secret_key" not in capsys.readouterr().out
