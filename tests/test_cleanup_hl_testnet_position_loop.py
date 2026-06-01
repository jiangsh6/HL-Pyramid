from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from scripts import cleanup_hl_testnet_position_loop as loop
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
from src.reporting.state_writer import read_state, write_state


def _cfg(tmp_path: Path):
    os.environ.setdefault("HL_TESTNET_ACCOUNT", "0x1111111111111111111111111111111111111111")
    os.environ.setdefault("HL_TESTNET_ACCOUNT_ADDRESS", "0x1111111111111111111111111111111111111111")
    cfg = load_config("config/btc_testnet_realistic_probe.yaml")
    cfg.bot["mode"] = "testnet"
    cfg.logging["run_dir"] = str(tmp_path)
    cfg.hl["network"] = "testnet"
    cfg.hl["coin"] = "BTC"
    cfg.hl["wallet_address"] = "0x1111111111111111111111111111111111111111"
    cfg.notifications["enabled"] = False
    return cfg


def _long_state(*, halted: bool = False, halt_reason: str | None = None) -> ThesisState:
    state = ThesisState(symbol="BTC", state=BotState.PYRAMID_LONG)
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
    state.halted = halted
    if halted:
        state.state = BotState.HALTED
        state.halt_reason = halt_reason
    return state


def _snapshot(*, qty: float = 0.005, open_orders: list[HLOpenOrder] | None = None) -> HLAccountSnapshot:
    positions = []
    if qty > 0:
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
        account_value=900.0,
        margin_used=10.0,
        withdrawable=890.0,
        positions=positions,
        perp_account_value=900.0,
        perp_margin_used=10.0,
        perp_positions=positions,
        open_orders_count=len(orders),
        open_orders=orders,
        effective_trading_collateral=900.0,
        trading_collateral_available=True,
    )


class _BookClient:
    def post_info(self, payload):
        assert payload == {"type": "l2Book", "coin": "BTC"}
        return {"levels": [[{"px": "71950.0"}], [{"px": "71960.0"}]]}


def _install_common(monkeypatch, tmp_path: Path, cfg, snapshots: list[HLAccountSnapshot]):
    write_state(_long_state(), str(tmp_path / "state.json"))
    monkeypatch.setattr(loop, "load_config", lambda *_: cfg)
    monkeypatch.setattr(loop, "get_private_key", lambda *_: "secret")
    monkeypatch.setattr(loop, "HyperliquidClient", lambda **_: _BookClient())
    monkeypatch.setattr(loop, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(
        loop,
        "reconcile_state_with_exchange",
        lambda state, snapshot, fills, config: (
            state,
            ReconciliationResult(status=ReconciliationStatus.OK, reason="ok"),
        ),
    )

    def fake_snapshot(*args, **kwargs):
        if len(snapshots) == 1:
            return snapshots[0]
        return snapshots.pop(0)

    monkeypatch.setattr(loop, "get_account_snapshot", fake_snapshot)


def test_loop_refuses_non_testnet(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    cfg.bot["mode"] = "paper"
    monkeypatch.setattr(loop, "load_config", lambda *_: cfg)

    rc = loop.main(["--config", "ignored.yaml", "--coin", "BTC", "--confirm"])

    assert rc == 2
    assert "REFUSED: testnet_only" in capsys.readouterr().out


def test_loop_refuses_local_exchange_mismatch(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    _install_common(monkeypatch, tmp_path, cfg, [_snapshot(qty=0.004)])

    rc = loop.main(["--config", "ignored.yaml", "--coin", "BTC", "--confirm"])

    assert rc == 1
    assert "REFUSED: local_exchange_qty_mismatch" in capsys.readouterr().out


def test_loop_clears_safe_cleanup_halt_and_attempts_ioc(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    write_state(
        _long_state(halted=True, halt_reason="cleanup_exit_failed: Order could not immediately match"),
        str(tmp_path / "state.json"),
    )
    monkeypatch.setattr(loop, "load_config", lambda *_: cfg)
    monkeypatch.setattr(loop, "get_private_key", lambda *_: "secret")
    monkeypatch.setattr(loop, "HyperliquidClient", lambda **_: _BookClient())
    monkeypatch.setattr(loop, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(
        loop,
        "reconcile_state_with_exchange",
        lambda state, snapshot, fills, config: (
            state,
            ReconciliationResult(status=ReconciliationStatus.OK, reason="ok"),
        ),
    )
    snapshots = [_snapshot(qty=0.005), _snapshot(qty=0.005)]
    monkeypatch.setattr(loop, "get_account_snapshot", lambda *a, **kw: snapshots.pop(0))

    def fake_execute(decision, state, config, client, wallet, private_key, mark_price):
        assert decision.action == ActionType.EXIT_ALL
        assert config.execution["time_in_force"] == "ioc"
        return OrderResult(
            status=OrderResultStatus.SUBMITTED_UNFILLED,
            action=ActionType.EXIT_ALL,
            side="sell",
            reduce_only=True,
            submitted_qty=0.005,
            filled_qty=0.0,
            remaining_qty=0.005,
            raw_status_sanitized="ioc_no_match",
            state_before=state.state.value,
            intended_state_after=BotState.EXITED.value,
            applied_state_after=state.state.value,
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(loop, "execute_hl", fake_execute)

    rc = loop.main(["--config", "ignored.yaml", "--coin", "BTC", "--max-attempts", "1", "--confirm"])

    state = read_state(str(tmp_path / "state.json"))
    assert rc == 1
    assert state.pending_order is None
    assert state.halt_reason == "ioc_flatten_unfilled_position_still_open"


def test_loop_cancels_exact_reduce_only_pending_order(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state = _long_state()
    state.pending_order = PendingOrder(
        oid=123,
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
    write_state(state, str(tmp_path / "state.json"))
    order = HLOpenOrder(oid=123, coin="BTC", side="sell", qty=0.005, limit_px=71900.0, status="open")
    snapshots = [_snapshot(qty=0.005, open_orders=[order]), _snapshot(qty=0.005)]
    canceled = {}
    monkeypatch.setattr(loop, "load_config", lambda *_: cfg)
    monkeypatch.setattr(loop, "get_private_key", lambda *_: "secret")
    monkeypatch.setattr(loop, "HyperliquidClient", lambda **_: _BookClient())
    monkeypatch.setattr(loop, "get_account_snapshot", lambda *a, **kw: snapshots.pop(0))
    monkeypatch.setattr(loop, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(
        loop,
        "reconcile_state_with_exchange",
        lambda state, snapshot, fills, config: (
            state,
            ReconciliationResult(status=ReconciliationStatus.OK, reason="ok"),
        ),
    )
    monkeypatch.setattr(loop, "cancel_order", lambda coin, oid, pk, client: canceled.setdefault("oid", oid) or True)

    rc = loop.main(["--config", "ignored.yaml", "--coin", "BTC", "--max-attempts", "1", "--confirm"])

    assert rc == 1
    assert canceled["oid"] == 123


def test_loop_refuses_open_order_without_matching_pending(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    order = HLOpenOrder(oid=999, coin="BTC", side="sell", qty=0.005, limit_px=71900.0, status="open")
    _install_common(monkeypatch, tmp_path, cfg, [_snapshot(qty=0.005, open_orders=[order])])

    rc = loop.main(["--config", "ignored.yaml", "--coin", "BTC", "--confirm"])

    assert rc == 1
    assert "REFUSED: unexpected_open_order_without_local_pending" in capsys.readouterr().out


def test_loop_handles_ioc_no_fill_without_pending_order(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _install_common(monkeypatch, tmp_path, cfg, [_snapshot(qty=0.005), _snapshot(qty=0.005)])
    monkeypatch.setattr(
        loop,
        "execute_hl",
        lambda *a, **kw: OrderResult(
            status=OrderResultStatus.REJECTED,
            action=ActionType.EXIT_ALL,
            side="sell",
            reduce_only=True,
            submitted_qty=0.005,
            filled_qty=0.0,
            remaining_qty=0.005,
            raw_status_sanitized="Order could not immediately match",
            state_before=BotState.PYRAMID_LONG.value,
            intended_state_after=BotState.EXITED.value,
            applied_state_after=BotState.PYRAMID_LONG.value,
            created_at=datetime.now(timezone.utc),
        ),
    )

    rc = loop.main(["--config", "ignored.yaml", "--coin", "BTC", "--max-attempts", "1", "--confirm"])

    state = read_state(str(tmp_path / "state.json"))
    assert rc == 1
    assert state.pending_order is None
    assert state.halted is True


def test_loop_retries_after_partial_ioc_fill(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _install_common(
        monkeypatch,
        tmp_path,
        cfg,
        [_snapshot(qty=0.005), _snapshot(qty=0.003), _snapshot(qty=0.003), _snapshot(qty=0.0)],
    )
    results = [
        OrderResult(
            status=OrderResultStatus.PARTIALLY_FILLED,
            action=ActionType.EXIT_ALL,
            side="sell",
            reduce_only=True,
            submitted_qty=0.005,
            filled_qty=0.002,
            remaining_qty=0.003,
            avg_fill_px=71900.0,
            state_before=BotState.PYRAMID_LONG.value,
            intended_state_after=BotState.EXITED.value,
            applied_state_after=BotState.PYRAMID_LONG.value,
            created_at=datetime.now(timezone.utc),
        ),
        OrderResult(
            status=OrderResultStatus.FILLED,
            action=ActionType.EXIT_ALL,
            side="sell",
            reduce_only=True,
            submitted_qty=0.003,
            filled_qty=0.003,
            remaining_qty=0.0,
            avg_fill_px=71900.0,
            state_before=BotState.PYRAMID_LONG.value,
            intended_state_after=BotState.EXITED.value,
            applied_state_after=BotState.EXITED.value,
            created_at=datetime.now(timezone.utc),
        ),
    ]

    def fake_execute(*args, **kwargs):
        return results.pop(0)

    monkeypatch.setattr(loop, "execute_hl", fake_execute)

    rc = loop.main(["--config", "ignored.yaml", "--coin", "BTC", "--max-attempts", "2", "--confirm"])

    state = read_state(str(tmp_path / "state.json"))
    assert rc == 0
    assert state.current_position_qty == 0.0
    assert state.state == BotState.EXITED
    assert state.pending_order is None


def test_loop_flat_when_qty_reaches_zero(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _install_common(monkeypatch, tmp_path, cfg, [_snapshot(qty=0.005), _snapshot(qty=0.0)])
    monkeypatch.setattr(
        loop,
        "execute_hl",
        lambda *a, **kw: OrderResult(
            status=OrderResultStatus.FILLED,
            action=ActionType.EXIT_ALL,
            side="sell",
            reduce_only=True,
            submitted_qty=0.005,
            filled_qty=0.005,
            remaining_qty=0.0,
            avg_fill_px=71900.0,
            state_before=BotState.PYRAMID_LONG.value,
            intended_state_after=BotState.EXITED.value,
            applied_state_after=BotState.EXITED.value,
            created_at=datetime.now(timezone.utc),
        ),
    )

    rc = loop.main(["--config", "ignored.yaml", "--coin", "BTC", "--confirm"])

    state = read_state(str(tmp_path / "state.json"))
    assert rc == 0
    assert state.current_position_qty == 0.0
    assert state.state == BotState.EXITED
