from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from scripts import cleanup_hl_dust_position, flatten_hl_position, reset_state
from src.core.config_loader import load_config
from src.core.models import ActionType, BotState, LotRecord, OrderResult, OrderResultStatus, PendingOrder, ThesisState
from src.execution.reconciler import reconcile_state_with_exchange
from src.hl.account import HLAccountSnapshot, HLFill, HLOpenOrder, HLPosition
from src.reporting.state_writer import read_state, update_dust_state, write_state


def _cfg(tmp_path: Path):
    cfg = load_config("config/mu_long_thesis.yaml")
    cfg.bot["mode"] = "testnet"
    cfg.data["source"] = "hyperliquid"
    cfg.logging["run_dir"] = str(tmp_path)
    cfg.hl = {
        "network": "testnet",
        "coin": "BTC",
        "bar_interval": "4h",
        "wallet_address": "0x1111111111111111111111111111111111111111",
        "collateral_mode": "unified",
        "sz_decimals": 5,
        "min_size": 0.001,
        "dust_policy": {
            "enabled": True,
            "dust_qty_threshold": 0.001,
            "dust_notional_threshold_usd": 10,
            "action": "halt_and_operator_review",
            "allow_reduce_only_dust_close": False,
            "allow_round_up_to_min_size_for_reduce_only": False,
        },
    }
    return cfg


def _dust_state() -> ThesisState:
    state = ThesisState(symbol="BTC", state=BotState.STARTER_LONG)
    state.base_lot = LotRecord(
        lot_id="base",
        entry_price=71860.0,
        qty=0.00025,
        entry_date=date(2026, 5, 31),
    )
    state.current_position_qty = 0.00025
    state.original_base_qty = 0.00125
    state.avg_entry_price = 71860.0
    state.sz_decimals = 5
    return state


def _target_runner_halted_state() -> ThesisState:
    state = ThesisState(symbol="BTC", state=BotState.HALTED, halted=True)
    state.halt_reason = "exit_order_canceled_position_still_open"
    state.base_lot = LotRecord(
        lot_id="base",
        entry_price=72380.0,
        qty=0.005,
        entry_date=date(2026, 5, 31),
    )
    state.current_position_qty = 0.005
    state.original_base_qty = 0.005
    state.avg_entry_price = 72380.0
    state.sz_decimals = 5
    return state


def _snapshot(
    *,
    position_qty: float = 0.00025,
    open_orders: list[HLOpenOrder] | None = None,
) -> HLAccountSnapshot:
    positions = []
    if position_qty > 0:
        positions = [
            HLPosition(
                coin="BTC",
                qty=position_qty,
                entry_price=71860.0,
                mark_price=71846.0,
                unrealized_pnl=0.0,
                liquidation_price=None,
                margin_used=6.0,
                leverage=1.0,
            )
        ]
    open_orders = open_orders or []
    return HLAccountSnapshot(
        account_value=6.0,
        margin_used=0.0,
        withdrawable=0.0,
        positions=positions,
        perp_account_value=6.0,
        perp_margin_used=0.0,
        perp_withdrawable=0.0,
        perp_positions=positions,
        spot_usdc_total=900.0,
        spot_usdc_hold=0.0,
        spot_usdc_available=900.0,
        open_orders_count=len(open_orders),
        open_orders=open_orders,
        collateral_mode="unified",
        effective_trading_collateral=900.0,
        trading_collateral_available=True,
        collateral_reason="unified_spot_usdc_available",
        perp_collateral_available=True,
    )


def _dust_pending() -> PendingOrder:
    return PendingOrder(
        oid=54042665660,
        symbol="BTC",
        action="exit_dust",
        side="sell",
        reduce_only=True,
        qty=0.00025,
        qty_submitted=0.00025,
        qty_filled=0.0,
        qty_remaining=0.00025,
        limit_px=72296.0,
        status="submitted_unfilled",
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        state_before="HALTED",
        intended_state_after="EXITED",
        applied_state_after="HALTED",
    )


def test_dust_qty_below_min_size_marks_and_halts_for_operator_review(tmp_path):
    cfg = _cfg(tmp_path)
    state = _dust_state()

    assert update_dust_state(state, 71846.0, cfg) is True
    assert state.dust_position is True
    assert state.dust_qty == 0.00025
    assert state.dust_notional == 0.00025 * 71846.0
    assert state.state == BotState.HALTED
    assert state.halt_reason == "dust_position_below_min_size"


def test_nonzero_dust_position_is_not_flat(tmp_path):
    cfg = _cfg(tmp_path)
    state = _dust_state()

    update_dust_state(state, 71846.0, cfg)

    assert state.current_position_qty > 0
    assert state.state != BotState.FLAT
    assert state.dust_position is True


def test_flatten_blocks_dust_before_order_and_writes_logs(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state_path = tmp_path / "state.json"
    write_state(_dust_state(), str(state_path))

    monkeypatch.setattr(flatten_hl_position, "load_config", lambda *_: cfg)
    monkeypatch.setattr(flatten_hl_position, "get_private_key", lambda *_: "secret")
    monkeypatch.setattr(flatten_hl_position, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(flatten_hl_position, "get_account_snapshot", lambda *a, **kw: _snapshot())
    monkeypatch.setattr(flatten_hl_position, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(
        flatten_hl_position,
        "execute_hl",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("order should not be submitted")),
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
            "--confirm",
            "--one-cycle",
        ],
    )

    rc = flatten_hl_position.main()

    persisted = read_state(str(state_path))
    assert rc == 1
    assert persisted.dust_position is True
    assert persisted.state == BotState.HALTED
    assert persisted.halt_reason == "dust_position_below_min_size"
    assert "blocked_pre_order" in (tmp_path / "orders.csv").read_text()
    assert "dust_position" in (tmp_path / "risk.csv").read_text()


def test_ioc_flatten_recovery_unfilled_leaves_no_pending_order(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state_path = tmp_path / "state.json"
    state = _target_runner_halted_state()
    state.state = BotState.BASE_LONG
    state.halted = False
    state.halt_reason = None
    write_state(state, str(state_path))

    def fake_execute(decision, state, config, client, wallet, private_key, mark_price):
        assert config.execution["time_in_force"] == "ioc"
        return OrderResult(
            status=OrderResultStatus.SUBMITTED_UNFILLED,
            action=ActionType.EXIT_ALL,
            side="sell",
            reduce_only=True,
            submitted_qty=decision.qty,
            filled_qty=0.0,
            remaining_qty=decision.qty,
            limit_px=72000.0,
            raw_status_sanitized="resting",
            state_before=state.state.value,
            intended_state_after=BotState.EXITED.value,
            applied_state_after=state.state.value,
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(flatten_hl_position, "load_config", lambda *_: cfg)
    monkeypatch.setattr(flatten_hl_position, "get_private_key", lambda *_: "secret")
    monkeypatch.setattr(flatten_hl_position, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(flatten_hl_position, "get_account_snapshot", lambda *a, **kw: _snapshot(position_qty=0.005))
    monkeypatch.setattr(flatten_hl_position, "get_recent_user_fills", lambda *a, **kw: [])
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
            "--recover-from-target-runner-validation-halt",
            "--time-in-force",
            "ioc",
            "--confirm",
            "--one-cycle",
        ],
    )

    rc = flatten_hl_position.main()

    persisted = read_state(str(state_path))
    assert rc == 1
    assert persisted.state == BotState.HALTED
    assert persisted.halt_reason == "ioc_flatten_unfilled_position_still_open"
    assert persisted.pending_order is None
    assert persisted.current_position_qty == 0.005


def test_ioc_flatten_partial_fill_does_not_record_pending_order(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    state_path = tmp_path / "state.json"
    write_state(_target_runner_halted_state(), str(state_path))

    def fake_execute(decision, state, config, client, wallet, private_key, mark_price):
        assert config.execution["time_in_force"] == "ioc"
        return OrderResult(
            status=OrderResultStatus.PARTIALLY_FILLED,
            action=ActionType.EXIT_ALL,
            side="sell",
            reduce_only=True,
            submitted_qty=decision.qty,
            filled_qty=0.002,
            remaining_qty=max(decision.qty - 0.002, 0.0),
            avg_fill_px=72000.0,
            limit_px=72000.0,
            raw_status_sanitized="partially_filled",
            state_before=state.state.value,
            intended_state_after=BotState.EXITED.value,
            applied_state_after=state.state.value,
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(flatten_hl_position, "load_config", lambda *_: cfg)
    monkeypatch.setattr(flatten_hl_position, "get_private_key", lambda *_: "secret")
    monkeypatch.setattr(flatten_hl_position, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(flatten_hl_position, "get_account_snapshot", lambda *a, **kw: _snapshot(position_qty=0.005))
    monkeypatch.setattr(flatten_hl_position, "get_recent_user_fills", lambda *a, **kw: [])
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
            "--recover-from-target-runner-validation-halt",
            "--time-in-force",
            "ioc",
            "--confirm",
            "--one-cycle",
        ],
    )

    rc = flatten_hl_position.main()

    persisted = read_state(str(state_path))
    assert rc == 1
    assert persisted.state == BotState.HALTED
    assert persisted.halt_reason == "residual_position_after_ioc_emergency_exit"
    assert persisted.pending_order is None
    assert persisted.current_position_qty == pytest.approx(0.003)


def test_flatten_aggressiveness_alias_overrides_legacy_aggressiveness(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    state_path = tmp_path / "state.json"
    write_state(_target_runner_halted_state(), str(state_path))
    observed = {}

    def fake_execute(decision, state, config, client, wallet, private_key, mark_price):
        observed["aggr"] = config.execution["exit_price_aggressiveness_bps"]
        return OrderResult(
            status=OrderResultStatus.SUBMITTED_UNFILLED,
            action=ActionType.EXIT_ALL,
            side="sell",
            reduce_only=True,
            submitted_qty=decision.qty,
            filled_qty=0.0,
            remaining_qty=decision.qty,
            limit_px=72000.0,
            raw_status_sanitized="submitted_unfilled",
            state_before=state.state.value,
            intended_state_after=BotState.EXITED.value,
            applied_state_after=state.state.value,
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(flatten_hl_position, "load_config", lambda *_: cfg)
    monkeypatch.setattr(flatten_hl_position, "get_private_key", lambda *_: "secret")
    monkeypatch.setattr(flatten_hl_position, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(flatten_hl_position, "get_account_snapshot", lambda *a, **kw: _snapshot(position_qty=0.005))
    monkeypatch.setattr(flatten_hl_position, "get_recent_user_fills", lambda *a, **kw: [])
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
            "--recover-from-target-runner-validation-halt",
            "--time-in-force",
            "ioc",
            "--exit-aggressiveness-bps",
            "2",
            "--aggressiveness-bps",
            "10",
            "--confirm",
            "--one-cycle",
        ],
    )

    flatten_hl_position.main()

    assert observed["aggr"] == pytest.approx(10.0)
    assert "exit_aggressiveness_bps=10.0" in capsys.readouterr().out


def test_marketable_exit_reference_sell_crosses_best_bid():
    class FakeClient:
        def post_info(self, payload):
            assert payload == {"type": "l2Book", "coin": "BTC"}
            return {"levels": [[{"px": "71000.0"}], [{"px": "71010.0"}]]}

    px, best_bid, best_ask = flatten_hl_position._marketable_exit_reference_price(
        FakeClient(),
        coin="BTC",
        side="sell",
        cross_bps=10,
    )

    assert best_bid == pytest.approx(71000.0)
    assert best_ask == pytest.approx(71010.0)
    assert px == pytest.approx(70929.0)
    assert px < best_bid


def test_marketable_exit_reference_buy_crosses_best_ask():
    class FakeClient:
        def post_info(self, payload):
            assert payload == {"type": "l2Book", "coin": "BTC"}
            return {"levels": [[["71000.0", "0.1"]], [["71010.0", "0.1"]]]}

    px, best_bid, best_ask = flatten_hl_position._marketable_exit_reference_price(
        FakeClient(),
        coin="BTC",
        side="buy",
        cross_bps=10,
    )

    assert best_bid == pytest.approx(71000.0)
    assert best_ask == pytest.approx(71010.0)
    assert px == pytest.approx(71081.01)
    assert px > best_ask


def test_marketable_from_book_requires_ioc(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    state_path = tmp_path / "state.json"
    write_state(_target_runner_halted_state(), str(state_path))
    executed = {"called": False}

    monkeypatch.setattr(flatten_hl_position, "load_config", lambda *_: cfg)
    monkeypatch.setattr(flatten_hl_position, "get_private_key", lambda *_: "secret")
    monkeypatch.setattr(flatten_hl_position, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(flatten_hl_position, "get_account_snapshot", lambda *a, **kw: _snapshot(position_qty=0.005))
    monkeypatch.setattr(flatten_hl_position, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(flatten_hl_position, "execute_hl", lambda *a, **kw: executed.__setitem__("called", True))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "flatten_hl_position.py",
            "--config",
            "ignored.yaml",
            "--coin",
            "BTC",
            "--recover-from-target-runner-validation-halt",
            "--marketable-from-book",
            "--confirm",
            "--one-cycle",
        ],
    )

    rc = flatten_hl_position.main()

    assert rc == 1
    assert executed["called"] is False
    assert "STOPPED_BEFORE_EXIT: marketable_from_book_requires_ioc" in capsys.readouterr().out


def test_ioc_marketable_from_book_uses_bid_reference_and_emergency_cap(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    state_path = tmp_path / "state.json"
    write_state(_target_runner_halted_state(), str(state_path))
    observed: dict[str, float | str | bool] = {}

    class FakeClient:
        def post_info(self, payload):
            assert payload == {"type": "l2Book", "coin": "BTC"}
            return {"levels": [[{"px": "71050.0"}], [{"px": "71060.0"}]]}

    def fake_execute(decision, state, config, client, wallet, private_key, mark_price):
        assert decision.action == ActionType.EXIT_ALL
        observed["mark_price"] = mark_price
        observed["tif"] = config.execution["time_in_force"]
        observed["aggr"] = config.execution["exit_price_aggressiveness_bps"]
        observed["max_bps"] = config.execution["max_oracle_deviation_bps"]
        return OrderResult(
            status=OrderResultStatus.SUBMITTED_UNFILLED,
            action=ActionType.EXIT_ALL,
            side="sell",
            reduce_only=True,
            submitted_qty=decision.qty,
            filled_qty=0.0,
            remaining_qty=decision.qty,
            limit_px=mark_price,
            raw_status_sanitized="ioc_no_match",
            state_before=state.state.value,
            intended_state_after=BotState.EXITED.value,
            applied_state_after=state.state.value,
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(flatten_hl_position, "load_config", lambda *_: cfg)
    monkeypatch.setattr(flatten_hl_position, "get_private_key", lambda *_: "secret")
    monkeypatch.setattr(flatten_hl_position, "HyperliquidClient", lambda **_: FakeClient())
    monkeypatch.setattr(flatten_hl_position, "get_account_snapshot", lambda *a, **kw: _snapshot(position_qty=0.005))
    monkeypatch.setattr(flatten_hl_position, "get_recent_user_fills", lambda *a, **kw: [])
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
            "--recover-from-target-runner-validation-halt",
            "--time-in-force",
            "ioc",
            "--aggressiveness-bps",
            "10",
            "--marketable-from-book",
            "--allow-emergency-deviation-bps",
            "25",
            "--confirm",
            "--one-cycle",
        ],
    )

    rc = flatten_hl_position.main()

    persisted = read_state(str(state_path))
    out = capsys.readouterr().out
    assert rc == 1
    assert observed["mark_price"] == pytest.approx(70978.95)
    assert observed["tif"] == "ioc"
    assert observed["aggr"] == pytest.approx(0.0)
    assert observed["max_bps"] == pytest.approx(25.0)
    assert persisted.pending_order is None
    assert persisted.halt_reason == "ioc_flatten_unfilled_position_still_open"
    assert "marketable_from_book=True" in out
    assert "book_best_bid=71050.0" in out
    assert "max_deviation_bps=25.0" in out


def test_marketable_from_book_refuses_mainnet_even_with_recovery_gates(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    cfg.bot["mode"] = "mainnet"
    cfg.bot["mainnet_confirmed"] = True
    cfg.hl["network"] = "mainnet"
    state_path = tmp_path / "state.json"
    state = _target_runner_halted_state()
    state.state = BotState.BASE_LONG
    state.halted = False
    state.halt_reason = None
    write_state(state, str(state_path))
    executed = {"called": False}

    monkeypatch.setenv("HL_ALLOW_MAINNET", "true")
    monkeypatch.setattr(flatten_hl_position, "load_config", lambda *_: cfg)
    monkeypatch.setattr(flatten_hl_position, "get_private_key", lambda *_: "secret")
    monkeypatch.setattr(flatten_hl_position, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(flatten_hl_position, "get_account_snapshot", lambda *a, **kw: _snapshot(position_qty=0.005))
    monkeypatch.setattr(flatten_hl_position, "get_recent_user_fills", lambda *a, **kw: [])
    monkeypatch.setattr(flatten_hl_position, "execute_hl", lambda *a, **kw: executed.__setitem__("called", True))
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
            "--recover-from-target-runner-validation-halt",
            "--time-in-force",
            "ioc",
            "--marketable-from-book",
            "--confirm",
            "--one-cycle",
        ],
    )

    rc = flatten_hl_position.main()

    assert rc == 1
    assert executed["called"] is False
    assert "STOPPED_BEFORE_EXIT: marketable_from_book_testnet_only" in capsys.readouterr().out


def test_reset_state_refuses_local_position(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    write_state(_dust_state(), str(tmp_path / "state.json"))
    monkeypatch.setattr(reset_state, "load_config", lambda *_: cfg)

    rc = reset_state.main(["--config", "ignored.yaml", "--confirm"])

    assert rc == 1
    assert "refusing reset" in capsys.readouterr().err
    assert read_state(str(tmp_path / "state.json")).current_position_qty == 0.00025


def test_reset_state_refuses_exchange_position_when_local_flat(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    write_state(ThesisState(symbol="BTC"), str(tmp_path / "state.json"))
    monkeypatch.setattr(reset_state, "load_config", lambda *_: cfg)
    monkeypatch.setattr(reset_state, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(reset_state, "get_account_snapshot", lambda *a, **kw: _snapshot(position_qty=0.00025))

    rc = reset_state.main(["--config", "ignored.yaml", "--confirm"])

    assert rc == 1
    assert "exchange still has a position" in capsys.readouterr().err


def test_reset_state_refuses_exchange_open_order_when_local_flat(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    write_state(ThesisState(symbol="BTC"), str(tmp_path / "state.json"))
    order = HLOpenOrder(oid=1, coin="BTC", side="buy", qty=0.001, limit_px=72000.0)
    monkeypatch.setattr(reset_state, "load_config", lambda *_: cfg)
    monkeypatch.setattr(reset_state, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(reset_state, "get_account_snapshot", lambda *a, **kw: _snapshot(position_qty=0.0, open_orders=[order]))

    rc = reset_state.main(["--config", "ignored.yaml", "--confirm"])

    assert rc == 1
    assert "exchange still has a position" in capsys.readouterr().err


def test_reset_state_succeeds_only_when_local_and_exchange_flat(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    halted = ThesisState(symbol="BTC", state=BotState.HALTED, halted=True, halt_reason="manual")
    write_state(halted, str(tmp_path / "state.json"))
    monkeypatch.setattr(reset_state, "load_config", lambda *_: cfg)
    monkeypatch.setattr(reset_state, "HyperliquidClient", lambda **_: object())
    monkeypatch.setattr(reset_state, "get_account_snapshot", lambda *a, **kw: _snapshot(position_qty=0.0))

    rc = reset_state.main(["--config", "ignored.yaml", "--confirm"])

    assert rc == 0
    assert read_state(str(tmp_path / "state.json")).state == BotState.FLAT


def test_dust_cleanup_guard_allows_exact_testnet_dust_only(tmp_path):
    cfg = _cfg(tmp_path)
    state = _dust_state()
    update_dust_state(state, 71846.0, cfg)

    assert cleanup_hl_dust_position.can_attempt_dust_close(
        state=state,
        network="testnet",
        confirmed=True,
        open_orders_count=0,
        min_size=0.001,
    )


def test_dust_cleanup_guard_refuses_non_dust_or_open_orders(tmp_path):
    cfg = _cfg(tmp_path)
    state = _dust_state()
    update_dust_state(state, 71846.0, cfg)

    assert not cleanup_hl_dust_position.can_attempt_dust_close(
        state=state,
        network="mainnet",
        confirmed=True,
        open_orders_count=0,
        min_size=0.001,
    )
    assert not cleanup_hl_dust_position.can_attempt_dust_close(
        state=state,
        network="testnet",
        confirmed=False,
        open_orders_count=0,
        min_size=0.001,
    )
    assert not cleanup_hl_dust_position.can_attempt_dust_close(
        state=state,
        network="testnet",
        confirmed=True,
        open_orders_count=1,
        min_size=0.001,
    )
    state.current_position_qty = 0.001
    assert not cleanup_hl_dust_position.can_attempt_dust_close(
        state=state,
        network="testnet",
        confirmed=True,
        open_orders_count=0,
        min_size=0.001,
    )


def test_reconciler_applies_filled_dust_exit_and_clears_pending(tmp_path):
    cfg = _cfg(tmp_path)
    state = _dust_state()
    update_dust_state(state, 71846.0, cfg)
    state.pending_order = _dust_pending()
    fill = HLFill(
        coin="BTC",
        side="sell",
        direction="Close Long",
        qty=0.00025,
        price=72296.0,
        timestamp_ms=1_780_000_000_000,
        oid=54042665660,
    )

    state, result = reconcile_state_with_exchange(
        state,
        _snapshot(position_qty=0.0),
        [fill],
        cfg,
    )

    assert result.reason == "reconciled_pending_reduce_from_recent_fills"
    assert state.current_position_qty == 0.0
    assert state.base_lot is None
    assert state.pending_order is None
    assert state.state == BotState.EXITED


def test_reconciler_refreshes_open_dust_exit_pending_order(tmp_path):
    cfg = _cfg(tmp_path)
    state = _dust_state()
    update_dust_state(state, 71846.0, cfg)
    state.pending_order = _dust_pending()
    order = HLOpenOrder(
        oid=54042665660,
        coin="BTC",
        side="sell",
        qty=0.00025,
        limit_px=72296.0,
        status="open",
    )

    state, result = reconcile_state_with_exchange(
        state,
        _snapshot(open_orders=[order]),
        [],
        cfg,
    )

    assert result.reason == "pending_order_still_open"
    assert state.pending_order is not None
    assert state.pending_order.action == "exit_dust"
    assert state.pending_order.qty_remaining == 0.00025
