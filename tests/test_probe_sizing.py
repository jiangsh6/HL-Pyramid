from __future__ import annotations

import pytest

from src.core.config_loader import load_config
from src.execution.probe_sizing import (
    get_probe_addon_qty,
    get_probe_base_qty,
    get_probe_runner_pct,
    validate_probe_qty_above_min,
    validate_runner_probe_sizes,
)
from src.strategy.sizing import calc_entry_size


@pytest.fixture(autouse=True)
def _stub_hl_env(monkeypatch):
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0x1111111111111111111111111111111111111111")
    monkeypatch.setenv("HL_TESTNET_AGENT_PRIVATE_KEY", "0x" + "1" * 64)


def test_realistic_probe_config_base_qty_is_above_5x_min_size():
    cfg = load_config("config/btc_testnet_realistic_probe.yaml")

    min_size = float(cfg.hl["min_size"])
    assert get_probe_base_qty(cfg, 72000.0) >= min_size * 5
    assert get_probe_addon_qty(cfg, 72000.0) >= min_size * 2


def test_runner_setup_with_realistic_qty_passes_validation():
    runner_target_qty, reduce_qty, blocker = validate_runner_probe_sizes(
        original_base_qty=0.005,
        current_qty=0.005,
        runner_pct=0.5,
        min_size=0.001,
    )

    assert blocker is None
    assert runner_target_qty == 0.0025
    assert reduce_qty == 0.0025


def test_runner_setup_with_small_qty_fails_validation():
    runner_target_qty, reduce_qty, blocker = validate_runner_probe_sizes(
        original_base_qty=0.00125,
        current_qty=0.00125,
        runner_pct=0.5,
        min_size=0.001,
    )

    assert runner_target_qty == 0.000625
    assert reduce_qty == 0.000625
    assert blocker == "target_runner_qty_below_min_size"


def test_explicit_probe_qty_overrides_config_default():
    cfg = load_config("config/btc_testnet_realistic_probe.yaml")

    assert get_probe_base_qty(cfg, 72000.0, explicit_qty=0.006) == 0.006
    assert get_probe_addon_qty(cfg, 72000.0, explicit_qty=0.003) == 0.003
    assert get_probe_runner_pct(cfg, 0.4) == 0.4


def test_probe_qty_refuses_too_small_without_edge_flag():
    blocker = validate_probe_qty_above_min(
        qty=0.00125,
        min_size=0.001,
        multiplier=5,
        allow_min_size_edge_test=False,
    )
    assert blocker == "probe_qty_below_realistic_threshold"


def test_probe_qty_allows_too_small_with_edge_flag():
    blocker = validate_probe_qty_above_min(
        qty=0.00125,
        min_size=0.001,
        multiplier=5,
        allow_min_size_edge_test=True,
    )
    assert blocker is None


def test_production_strategy_sizing_unchanged():
    cfg = load_config("config/btc_testnet_order_probe.yaml")
    qty, blocker = calc_entry_size(
        state=None,
        config=cfg,
        entry_price=72000.0,
        atr14=720.0,
        intended_exposure_pct=cfg.entry["starter"]["exposure_pct"],
        sz_decimals=cfg.hl["sz_decimals"],
    )

    assert blocker is None
    assert qty > 0.0
