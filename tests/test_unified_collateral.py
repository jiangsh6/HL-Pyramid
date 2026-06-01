"""
Unified-collateral tests — HL account snapshot + run_live_hl order gate.

Covers (T6 of unified-collateral refactor):
  test_unified_spot_usdc_counts_as_collateral
  test_clearinghouse_only_ignores_spot_usdc
  test_no_double_count_uses_max_not_sum
  test_order_gate_allows_when_unified_collateral_available
  test_order_gate_blocks_when_no_collateral
  test_diagnostic_output_includes_collateral_fields
  test_collateral_mode_validated_by_config_loader
  test_structured_rejection_keys_are_preserved
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts import run_live_hl as live_script
from src.core.config_loader import load_config, validate_config
from src.core.models import ActionType, BotState, Decision, ThesisState
from src.hl.account import (
    COLLATERAL_MODE_CLEARINGHOUSE_ONLY,
    COLLATERAL_MODE_UNIFIED,
    HLAccountSnapshot,
    get_account_snapshot,
)
from src.hl.order_placer import _parse_rejection_status

FAKE_KEY = "0x" + "a" * 64
TESTNET_WALLET = "0x1111111111111111111111111111111111111111"


def _mock_client_with_state(perp_account_value: float, spot_usdc_total: float):
    """Build a MagicMock HL client returning the configured perp + spot state."""
    client = MagicMock()

    def _post_info(payload):
        ptype = payload["type"]
        if ptype == "clearinghouseState":
            return {
                "marginSummary": {
                    "accountValue": str(perp_account_value),
                    "totalMarginUsed": "0.0",
                },
                "withdrawable": "0.0",
                "assetPositions": [],
            }
        if ptype == "spotClearinghouseState":
            return {
                "balances": [
                    {"coin": "USDC", "total": str(spot_usdc_total),
                     "hold": "0.0", "entryNtl": "0.0"},
                ]
            }
        if ptype == "subAccounts":
            return None
        return {}

    client.post_info.side_effect = _post_info
    return client


# ── Snapshot logic ────────────────────────────────────────────────────────────

def test_unified_spot_usdc_counts_as_collateral():
    """Under unified mode, spot USDC raises effective_trading_collateral."""
    client = _mock_client_with_state(perp_account_value=0.0, spot_usdc_total=972.84283)
    snap = get_account_snapshot("0xABC", client, collateral_mode=COLLATERAL_MODE_UNIFIED)

    assert snap.perp_account_value == 0.0
    assert snap.spot_usdc_available == pytest.approx(972.84283)
    assert snap.effective_trading_collateral == pytest.approx(972.84283)
    assert snap.trading_collateral_available is True
    assert snap.collateral_reason == "unified_spot_usdc_available"
    # Raw perp availability stays accurately reported.
    assert snap.perp_collateral_available is False


def test_clearinghouse_only_ignores_spot_usdc():
    """Under clearinghouse_only mode, spot USDC must NOT count as collateral."""
    client = _mock_client_with_state(perp_account_value=0.0, spot_usdc_total=972.84283)
    snap = get_account_snapshot(
        "0xABC", client, collateral_mode=COLLATERAL_MODE_CLEARINGHOUSE_ONLY,
    )

    assert snap.spot_usdc_available == pytest.approx(972.84283)  # raw still parsed
    assert snap.effective_trading_collateral == 0.0
    assert snap.trading_collateral_available is False
    assert snap.collateral_reason == "spot_balance_not_perp_margin"


def test_no_double_count_uses_max_not_sum():
    """perp=100 + spot=900 in unified mode → effective=900 (max), NOT 1000."""
    client = _mock_client_with_state(perp_account_value=100.0, spot_usdc_total=900.0)
    snap = get_account_snapshot("0xABC", client, collateral_mode=COLLATERAL_MODE_UNIFIED)

    assert snap.effective_trading_collateral == pytest.approx(900.0)
    assert snap.trading_collateral_available is True
    # max(perp, spot) == spot here, so reason is the spot variant
    assert snap.collateral_reason == "unified_spot_usdc_available"


def test_unified_perp_only_reports_perp_reason():
    """perp>0, spot=0 in unified mode → reason should reference perp account value."""
    client = _mock_client_with_state(perp_account_value=500.0, spot_usdc_total=0.0)
    snap = get_account_snapshot("0xABC", client, collateral_mode=COLLATERAL_MODE_UNIFIED)

    assert snap.effective_trading_collateral == 500.0
    assert snap.trading_collateral_available is True
    assert snap.collateral_reason == "unified_perp_account_value_available"


def test_no_unified_collateral_at_all_blocks():
    """No perp, no spot, unified mode → trading_collateral_available=False."""
    client = _mock_client_with_state(perp_account_value=0.0, spot_usdc_total=0.0)
    snap = get_account_snapshot("0xABC", client, collateral_mode=COLLATERAL_MODE_UNIFIED)

    assert snap.effective_trading_collateral == 0.0
    assert snap.trading_collateral_available is False
    assert snap.collateral_reason == "no_unified_collateral_available"


# ── Order gate behavior (run_decision_cycle) ──────────────────────────────────

def _make_unified_snapshot(spot_usdc: float = 972.84) -> HLAccountSnapshot:
    return HLAccountSnapshot(
        account_value=0.0,
        margin_used=0.0,
        withdrawable=0.0,
        positions=[],
        perp_account_value=0.0,
        perp_margin_used=0.0,
        perp_withdrawable=0.0,
        perp_positions=[],
        spot_usdc_total=spot_usdc,
        spot_usdc_hold=0.0,
        spot_usdc_available=spot_usdc,
        collateral_mode=COLLATERAL_MODE_UNIFIED,
        effective_trading_collateral=spot_usdc,
        trading_collateral_available=spot_usdc > 0,
        collateral_reason=("unified_spot_usdc_available" if spot_usdc > 0
                           else "no_unified_collateral_available"),
        perp_collateral_available=False,
    )


def _stub_cycle_deps(monkeypatch, hl_snapshot: HLAccountSnapshot, decision: Decision):
    """Stub fetch_ohlcv_hl, calc_indicators, get_account_snapshot, engine_run."""
    # fetch_ohlcv_hl: return a non-empty placeholder DataFrame
    fake_df = pd.DataFrame({"close": [50000.0]})
    monkeypatch.setattr(live_script, "fetch_ohlcv_hl", lambda *a, **kw: fake_df)

    # calc_indicators: return a snapshot with a usable close price
    class _Ind:
        date = __import__("datetime").date(2026, 5, 31)
        adj_close = 50000.0
        close = 50000.0
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
    monkeypatch.setattr(live_script, "calc_indicators", lambda *a, **kw: _Ind())

    monkeypatch.setattr(
        live_script, "get_account_snapshot",
        lambda *a, **kw: hl_snapshot,
    )
    monkeypatch.setattr(live_script, "engine_run", lambda *a, **kw: decision)
    # Funding stubs — keep state untouched
    monkeypatch.setattr(live_script, "apply_candle_close_funding",
                        lambda state, *a, **kw: state)
    # Suppress daily summary noise
    monkeypatch.setattr(live_script, "format_summary",
                        lambda *a, **kw: "")


def _testnet_config(tmp_path):
    cfg = load_config("config/mu_long_thesis.yaml")
    cfg.bot["mode"] = "testnet"
    cfg.data["source"] = "hyperliquid"
    cfg.logging["run_dir"] = str(tmp_path / "run")
    cfg.hl = {
        "network": "testnet",
        "coin": "BTC",
        "wallet_address": TESTNET_WALLET,
        "bar_interval": "4h",
        "sz_decimals": 5,
        "collateral_mode": COLLATERAL_MODE_UNIFIED,
    }
    return cfg


def test_order_gate_allows_when_unified_collateral_available(monkeypatch, tmp_path):
    """In unified mode with spot USDC, the gate does NOT block; broker is dispatched."""
    cfg = _testnet_config(tmp_path)
    snap = _make_unified_snapshot(spot_usdc=972.84)
    decision = Decision(action=ActionType.BUY_STARTER, qty=0.001, reason="test_starter")
    _stub_cycle_deps(monkeypatch, snap, decision)

    dispatch_calls = {"count": 0}

    def fake_dispatch(*args, **kwargs):
        dispatch_calls["count"] += 1
        from src.core.models import Fill
        from datetime import datetime
        return Fill(
            action=ActionType.BUY_STARTER, qty=0.001, fill_price=50000.0,
            slippage_bps=1.0, realized_pnl=0.0, commission=0.0,
            timestamp=datetime.now(),
        )
    monkeypatch.setattr(live_script, "_dispatch_broker", fake_dispatch)

    state = ThesisState(symbol="BTC")
    live_script._STOP_EVENT.clear()

    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json",
        client=object(), wallet_address=TESTNET_WALLET, private_key=FAKE_KEY,
        feed=None, dry_run=False,
    )

    assert dispatch_calls["count"] == 1, "broker dispatch must run under unified collateral"
    assert status in (live_script.CONTINUE, live_script.STOPPED)
    live_script._STOP_EVENT.clear()


def test_order_gate_blocks_when_no_collateral(monkeypatch, tmp_path):
    """With no perp and no spot collateral, the gate blocks before broker dispatch."""
    cfg = _testnet_config(tmp_path)
    snap = _make_unified_snapshot(spot_usdc=0.0)  # no collateral at all
    assert snap.trading_collateral_available is False
    decision = Decision(action=ActionType.BUY_STARTER, qty=0.001, reason="test_starter")
    _stub_cycle_deps(monkeypatch, snap, decision)

    dispatch_calls = {"count": 0}
    monkeypatch.setattr(
        live_script, "_dispatch_broker",
        lambda *a, **kw: dispatch_calls.__setitem__("count", dispatch_calls["count"] + 1),
    )

    state = ThesisState(symbol="BTC")
    live_script._STOP_EVENT.clear()

    status = live_script.run_decision_cycle(
        cfg, state, tmp_path / "state.json",
        client=object(), wallet_address=TESTNET_WALLET, private_key=FAKE_KEY,
        feed=None, dry_run=False,
    )

    assert status == live_script.BLOCKED_NO_COLLATERAL
    assert dispatch_calls["count"] == 0, "broker dispatch must NOT run without collateral"

    # All four CSV logs must have been written for the operator's record.
    run_dir = Path(cfg.logging["run_dir"])
    for csv_name in ("orders.csv", "positions.csv", "risk.csv", "decisions.csv"):
        assert (run_dir / csv_name).exists(), f"{csv_name} not written"

    # orders.csv reflects the blocked-before-order outcome.
    orders_text = (run_dir / "orders.csv").read_text()
    assert "no_order" in orders_text
    assert "insufficient_trading_collateral" in orders_text
    # risk.csv reflects blocked status with the blocker key.
    risk_text = (run_dir / "risk.csv").read_text()
    assert "blocked" in risk_text
    assert "insufficient_trading_collateral" in risk_text
    live_script._STOP_EVENT.clear()


# ── Config / diagnostic / rejection parsing ───────────────────────────────────

def test_collateral_mode_validated_by_config_loader():
    """Invalid collateral_mode is rejected at config-load time."""
    with patch.dict(os.environ, {"HL_TESTNET_ACCOUNT_ADDRESS": TESTNET_WALLET}, clear=False):
        cfg = load_config("config/btc_long_thesis.yaml")
    cfg.hl["collateral_mode"] = "invalid_mode"  # type: ignore[index]
    with pytest.raises(ValueError, match="hl.collateral_mode must be"):
        validate_config(cfg)


def test_diagnostic_output_includes_collateral_fields(capsys, monkeypatch):
    """diagnose_hl_account.main() prints the unified-collateral diagnostic fields."""
    from scripts import diagnose_hl_account as diag_script

    fake_snap = HLAccountSnapshot(
        account_value=0.0, margin_used=0.0, withdrawable=0.0,
        perp_account_value=0.0, perp_margin_used=0.0, perp_withdrawable=0.0,
        spot_usdc_total=972.84, spot_usdc_hold=0.0, spot_usdc_available=972.84,
        collateral_mode=COLLATERAL_MODE_UNIFIED,
        effective_trading_collateral=972.84,
        trading_collateral_available=True,
        collateral_reason="unified_spot_usdc_available",
        perp_collateral_available=False,
    )

    mock_client = MagicMock()
    mock_client.post_info.return_value = {}
    fake_signer = type("A", (), {"address": "0xAGENT00000000000000000000000000000000000"})

    monkeypatch.setattr(diag_script, "HyperliquidClient", lambda network: mock_client)
    monkeypatch.setattr(diag_script, "get_account_snapshot", lambda *a, **kw: fake_snap)
    monkeypatch.setattr(diag_script, "build_signer", lambda key: fake_signer)
    monkeypatch.setattr(diag_script, "get_private_key", lambda cfg: FAKE_KEY)

    monkeypatch.setattr(
        sys, "argv",
        ["diagnose_hl_account.py", "--config", "config/btc_testnet_order_probe.yaml"],
    )

    with patch.dict(os.environ, {
        "HL_TESTNET_ACCOUNT_ADDRESS": TESTNET_WALLET,
        "HL_TESTNET_AGENT_PRIVATE_KEY": FAKE_KEY,
    }):
        rc = diag_script.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "collateral_mode=unified" in out
    assert "effective_trading_collateral=972.84" in out
    assert "trading_collateral_available=True" in out
    assert "collateral_reason=unified_spot_usdc_available" in out
    assert "spot_usdc_available=972.84" in out
    # Mode-aware operator hint must fire for this fixture.
    assert "Unified collateral detected via spot USDC" in out
    # Secrets must NOT appear anywhere in the output.
    assert FAKE_KEY not in out
    assert FAKE_KEY.replace("0x", "") not in out


def test_structured_rejection_keys_are_preserved():
    """_parse_rejection_status surfaces the named HL rejection keys."""
    cases = [
        {"perpMarginRejected": {}},
        {"minTradeNtlRejected": {}},
        {"insufficientSpotBalanceRejected": {}},
        {"oracleRejected": {}},
        {"tickRejected": {}},
        {"badAloPxRejected": {}},
        {"postOnlyRejected": {}},
        {"reduceOnlyRejected": {}},
    ]
    for status in cases:
        key = next(iter(status))
        parsed = _parse_rejection_status(status)
        assert parsed is not None
        assert key in parsed, f"expected {key} in {parsed!r}"
